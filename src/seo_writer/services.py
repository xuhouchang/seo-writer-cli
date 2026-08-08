"""Business services: step execution with state-machine guards, idempotency,
retry policy, cost ledger, audit trail and evidence typing.

Every state-changing step follows the same skeleton:
  1. resolve context           4. run provider(s) with retry policy
  2. idempotency lookup        5. record costs + audit
  3. state/authorization guard 6. persist artifacts + transition status
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml as _yaml

from . import RULES_VERSION
from . import state_machine as sm
from .config import Workspace, load_yaml_model
from .db import Database
from .errors import (
    ApprovalInvalidatedError,
    ApprovalRequiredError,
    GateNotPassedError,
    NotFoundError,
    PermanentProviderError,
    SeoWriterError,
    TransientProviderError,
    UsageError,
    ValidationFailedError,
)
from .ids import hash_payload, idempotency_key, new_run_id, sha256_text
from .models import PolicyYaml, TopicYaml
from .providers import build_providers, llm_provider
from .providers.base import ProviderResult
from .validators.claim_safety import (
    validate_metadata_lengths,
    validate_outline_structure,
    validate_run_corpus,
)
from .validators.research_gate import evaluate as evaluate_gate

# Conservative per-call cost estimate used for the run budget pre-check.
MAX_CALL_COST = {"keyword": 0.001, "serp": 0.002, "webfetch": 0.002, "community": 0.003, "llm": 0.03}


# ---------------------------------------------------------------------------
# context resolution
# ---------------------------------------------------------------------------


def resolve_brand(db: Database, slug: str) -> dict:
    brand = db.get_brand(slug)
    if brand is None:
        raise NotFoundError(f"brand '{slug}' does not exist (create it with `seo-writer brand create`)")
    return brand


def resolve_project(db: Database, brand_slug: str, project_slug: str) -> tuple[dict, dict]:
    brand = resolve_brand(db, brand_slug)
    project = db.get_project(brand["id"], project_slug)
    if project is None:
        raise NotFoundError(f"project '{project_slug}' does not exist for brand '{brand_slug}'")
    return brand, project


def resolve_run(db: Database, run_id: str) -> dict:
    run = db.get_run(run_id)
    if run is None:
        raise NotFoundError(f"run '{run_id}' does not exist")
    return run


def load_policy(db: Database, brand_id: int) -> PolicyYaml:
    raw = db.get_policy(brand_id)
    if raw is None:
        raise UsageError("brand has no policy.yaml imported (`seo-writer brand policy import`)")
    return PolicyYaml.model_validate(raw)


def load_facts(db: Database, brand_id: int) -> dict:
    payload = db.latest_facts_payload(brand_id)
    if payload is None:
        raise UsageError("brand has no fact pack imported (`seo-writer brand facts import`)")
    return payload


def _resume_allowed(run: dict, step: str) -> None:
    """A blocked run may only resume the step that blocked it."""
    sm.assert_step_authorized(step, run["status"])
    if run["status"] == sm.BLOCKED and run.get("step") not in {step, "validate"}:
        raise SeoWriterError(
            f"run is blocked by step '{run.get('step')}'; use `run retry --step {run.get('step')}` first"
        )


def _check_budget(db: Database, run_id: str, policy: PolicyYaml, role: str) -> None:
    if db.cost_total(run_id) + MAX_CALL_COST[role] > policy.cost_limit_per_run:
        raise PermanentProviderError(
            role, "run cost limit exceeded; raise policy.cost_limit_per_run to continue"
        )


def _call(
    db: Database,
    run_id: str,
    policy: PolicyYaml,
    role: str,
    key: str,
    provider_name: str,
    provider_profile: str,
    operation: str,
    fn: Callable[[], ProviderResult],
    fingerprint: str = "",
) -> ProviderResult:
    """One provider call: budget check, bounded transient retries, cost record."""
    _check_budget(db, run_id, policy, role)
    attempts = 0
    while True:
        try:
            result = fn()
            break
        except TransientProviderError:
            attempts += 1
            if attempts > policy.retries:
                raise
            db.add_audit(
                run_id, "provider.retry", {"provider": provider_name, "op": operation, "attempt": attempts}
            )
    db.add_cost(
        {
            "run_id": run_id,
            "idempotency_key": f"{key}:{operation}",
            "provider": provider_name,
            "provider_profile": provider_profile,
            "operation": operation,
            "cost_estimate": result.cost_estimate,
            "token_estimate": result.token_estimate,
            "request_fingerprint": result.request_fingerprint or fingerprint,
        }
    )
    return result


def _fail_blocked(db: Database, run: dict, step: str, reason: str) -> None:
    db.set_status(run["id"], sm.BLOCKED, step=step, failure_reason=reason)
    db.add_audit(run["id"], "run.blocked", {"step": step, "reason": reason})


# ---------------------------------------------------------------------------
# brand + project + run creation
# ---------------------------------------------------------------------------


def create_project(db: Database, brand_slug: str, slug: str, title: str | None = None) -> dict:
    brand = resolve_brand(db, brand_slug)
    if db.get_project(brand["id"], slug):
        raise UsageError(f"project '{slug}' already exists for brand '{brand_slug}'")
    project = db.create_project(brand["id"], slug, title or slug)
    db.add_audit(None, "project.created", {"brand": brand_slug, "project": slug})
    return {"brand": brand_slug, "project": project}


def create_run(
    ws: Workspace,
    db: Database,
    brand_slug: str,
    project_slug: str,
    brief_file: str,
) -> dict:
    brand, project = resolve_project(db, brand_slug, project_slug)
    policy = load_policy(db, brand["id"])
    facts = load_facts(db, brand["id"])
    topic = load_yaml_model(Path(brief_file), TopicYaml, "topic.yaml")
    brief = {
        **topic.model_dump(),
        "brand_name": brand["name"],
        "brand_slug": brand_slug,
        "project": project_slug,
        "policy_hash": hash_payload(policy.model_dump()),
    }
    run_id = new_run_id()
    db.create_run(run_id, project["id"], brief, facts["facts_hash"], facts["facts_version"])
    ws.write_json(run_id, "brief.json", brief)
    ws.write_json(run_id, "facts-snapshot.json", facts)
    ws.write_text(run_id, "topic.yaml", Path(brief_file).read_text(encoding="utf-8"))
    db.add_audit(run_id, "run.created", {"brief": brief, "facts_hash": facts["facts_hash"]})
    return {"run_id": run_id, "status": sm.CREATED, "brief": brief, "facts_hash": facts["facts_hash"]}


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------


def _evidence_row(**kw: Any) -> dict[str, Any]:
    return {
        "evidence_id": kw["evidence_id"],
        "source_type": kw.get("source_type", "unknown"),
        "fetch_method": kw.get("fetch_method", "snippet_only"),
        "opened_current_run": kw.get("opened_current_run", False),
        "evidence_origin": kw.get("evidence_origin", "structured_discovery"),
        "platform": kw.get("platform"),
        "url": kw.get("url"),
        "title": kw.get("title"),
        "grade": kw.get("grade"),
        "summary": kw.get("summary", ""),
        "details": kw.get("details", {}),
    }


def run_research(
    db: Database, run: dict, policy: PolicyYaml, providers: dict | None = None, key: str | None = None
) -> dict:
    _resume_allowed(run, "research")
    run_id = run["id"]
    key = key or idempotency_key(run_id, "research")
    prior = db.get_command_result(run_id, "research", key)
    if prior is not None:
        return prior
    providers = providers or build_providers(policy)
    brief = json.loads(run["brief_snapshot"])
    kw = providers["keyword"]
    serp = providers["serp"]
    fetch = providers["webfetch"]
    community = providers["community"]
    rows: list[dict[str, Any]] = []
    try:
        primary = brief["seed_keywords"][0]

        metrics = _call(
            db,
            run_id,
            policy,
            "keyword",
            key,
            kw.name,
            kw.profile,
            "keyword.search_volume",
            lambda: kw.search_volume(brief["seed_keywords"]),
        )
        related = _call(
            db,
            run_id,
            policy,
            "keyword",
            key,
            kw.name,
            kw.profile,
            "keyword.related",
            lambda: kw.related(primary),
        )
        paa = _call(
            db, run_id, policy, "keyword", key, kw.name, kw.profile, "keyword.paa", lambda: kw.paa(primary)
        )
        for i, k in enumerate(brief["seed_keywords"]):
            rows.append(
                _evidence_row(
                    evidence_id=f"KW-{i + 1:02d}",
                    source_type="keyword",
                    fetch_method="mock_api",
                    opened_current_run=False,
                    evidence_origin="structured_discovery",
                    title=k,
                    details={"volume": metrics.data.get(k, {}).get("volume")},
                )
            )
        rows.append(
            _evidence_row(
                evidence_id="KW-REL-01",
                source_type="keyword",
                fetch_method="mock_api",
                opened_current_run=False,
                evidence_origin="structured_discovery",
                title="related keywords",
                details={"related": related.data},
            )
        )
        rows.append(
            _evidence_row(
                evidence_id="KW-PAA-01",
                source_type="keyword",
                fetch_method="mock_api",
                opened_current_run=False,
                evidence_origin="structured_discovery",
                title="PAA candidates",
                details={"paa": paa.data},
            )
        )

        query_variants = [
            ("head", "exact/head"),
            ("howto", "how-to/workflow"),
            ("decision", "decision/comparison/failure"),
        ]
        organic_by_rank: dict[int, dict[str, Any]] = {}
        for i, (variant, label) in enumerate(query_variants):
            q = _call(
                db,
                run_id,
                policy,
                "serp",
                key,
                serp.name,
                serp.profile,
                f"serp.query:{variant}",
                lambda v=variant: serp.query(primary, v),
            )
            organic = q.data.get("organic", [])
            for r in organic:
                organic_by_rank.setdefault(r["rank"], r)
            rows.append(
                _evidence_row(
                    evidence_id=f"Q-{i + 1:02d}",
                    source_type="search_query",
                    fetch_method="mock_api",
                    opened_current_run=False,
                    evidence_origin="structured_discovery",
                    title=q.data["query"],
                    grade=label,
                    details={
                        "query": q.data["query"],
                        "query_method": "mock_api",
                        "timestamp": q.timestamp,
                        "location_language_device": "US / en / desktop",
                        "observation_method": "mock_api",
                        "aio_visible": q.data.get("aio_visible"),
                        "aio_conclusion": q.data.get("aio_conclusion"),
                        "paa": q.data.get("paa"),
                        "related_searches": q.data.get("related_searches"),
                        "top_10": [
                            {"rank": r["rank"], "url": r["url"], "title": r["title"]} for r in organic
                        ],
                    },
                )
            )
        # open the top distinct pages (current-run opened, body fetched)
        open_count = int(getattr(serp, "fixture", {}).get("open_pages", 5))
        for opened, rank in enumerate(sorted(organic_by_rank)):
            if opened >= open_count:
                break
            url = organic_by_rank[rank]["url"]
            page = _call(
                db,
                run_id,
                policy,
                "webfetch",
                key,
                fetch.name,
                fetch.profile,
                "webfetch.fetch",
                lambda u=url: fetch.fetch(u),
            )
            data = page.data
            rows.append(
                _evidence_row(
                    evidence_id=f"SERP-{opened + 1:02d}",
                    source_type="serp_page",
                    fetch_method="mock_webfetch",
                    opened_current_run=True,
                    evidence_origin="current_run",
                    platform="Google SERP",
                    url=url,
                    title=data["title"],
                    grade="editorial guide",
                    summary=" | ".join(data["headings"]) + " :: " + " | ".join(data["main_claims"]),
                    details={"limitations": data["limitations"], "content_type": data["content_type"]},
                )
            )

        disc = _call(
            db,
            run_id,
            policy,
            "community",
            key,
            community.name,
            community.profile,
            "community.discover",
            lambda: community.discover(primary),
        )
        candidates = disc.data
        fixture = getattr(community, "fixture", {})
        for i, c in enumerate(candidates):
            rows.append(
                _evidence_row(
                    evidence_id=f"THREAD-CAND-{i + 1:02d}",
                    source_type="community_thread",
                    fetch_method="mock_api",
                    opened_current_run=False,
                    evidence_origin="structured_discovery",
                    platform=c["platform"],
                    url=c["url"],
                    title=c["title"],
                    grade="candidate",
                )
            )
        open_threads = int(fixture.get("open_threads", 10))
        second_platform_threads = int(fixture.get("second_platform_threads", 1))
        document_insufficiency = bool(fixture.get("document_second_platform_insufficiency", False))
        for n, c in enumerate(candidates):
            if n >= open_threads:
                break
            thread = _call(
                db,
                run_id,
                policy,
                "community",
                key,
                community.name,
                community.profile,
                "community.read_thread",
                lambda u=c["url"], p=c["platform"]: community.read_thread(u, p),
            )
            data = thread.data
            rows.append(
                _evidence_row(
                    evidence_id=f"THREAD-{n + 1:02d}",
                    source_type="community_thread",
                    fetch_method="mock_reddit",
                    opened_current_run=True,
                    evidence_origin="current_run",
                    platform=data["platform"],
                    url=data["url"],
                    title=data["title"],
                    grade=data["grade"],
                    summary=f"{data['original_post_summary']} | counterexample: {data['counterexample']}",
                    details={
                        "high_signal_comments": data["high_signal_comments_read"],
                        "promotion_disclosure": data["promotion_disclosure"],
                    },
                )
            )
        for j in range(second_platform_threads):
            sp = _call(
                db,
                run_id,
                policy,
                "community",
                key,
                community.name,
                community.profile,
                "community.read_thread",
                lambda: community.second_platform_thread(),
            )
            data = sp.data
            rows.append(
                _evidence_row(
                    evidence_id=f"THREAD-SP-{j + 1:02d}",
                    source_type="community_thread",
                    fetch_method="mock_reddit",
                    opened_current_run=True,
                    evidence_origin="current_run",
                    platform=data["platform"],
                    url=data["url"],
                    title=data["title"],
                    grade=data["grade"],
                    summary=data["original_post_summary"],
                )
            )
        if document_insufficiency:
            rows.append(
                _evidence_row(
                    evidence_id="THREAD-SECOND-PLATFORM-SEARCH",
                    source_type="community_thread",
                    fetch_method="mock_api",
                    opened_current_run=False,
                    evidence_origin="structured_discovery",
                    platform="Quora",
                    title="second-platform search",
                    details={"second_platform_search_outcome": "insufficient"},
                )
            )

        db.replace_run_evidence(run_id, rows)
        db.set_status(run_id, sm.RESEARCHING, step="research", failure_reason=None)
        db.add_audit(
            run_id,
            "research.completed",
            {"evidence_rows": len(rows), "opened": sum(1 for r in rows if r["opened_current_run"])},
        )
        result = {
            "run_id": run_id,
            "status": sm.RESEARCHING,
            "step": "research",
            "evidence_rows": len(rows),
            "opened_current_run": sum(1 for r in rows if r["opened_current_run"]),
        }
        db.record_command(run_id, "research", key, result)
        return result
    except (PermanentProviderError, TransientProviderError) as exc:
        _fail_blocked(db, run, "research", f"{exc} (retryable={exc.retryable})")
        raise


# ---------------------------------------------------------------------------
# gate + outline + approval
# ---------------------------------------------------------------------------


def validate_research(db: Database, run: dict, policy: PolicyYaml) -> dict:
    _resume_allowed(run, "validate_research")
    run_id = run["id"]
    if run["status"] == sm.GATE_PASSED and run.get("step") != "research":
        # already passed; blocked runs fall through so the gate re-evaluates
        return {"run_id": run_id, "status": sm.GATE_PASSED, "passed": True, "gaps": []}
    report = evaluate_gate(db.list_evidence(run_id), policy.research_gate)
    if not report.passed:
        db.set_status(run_id, sm.BLOCKED, step="research", failure_reason="; ".join(report.errors))
        db.add_audit(
            run_id, "research_gate.failed", {"errors": report.errors, "rules_version": RULES_VERSION}
        )
        raise ValidationFailedError(report.errors, step="validate_research")
    db.set_status(run_id, sm.GATE_PASSED, step="research", failure_reason=None)
    db.add_audit(run_id, "research_gate.passed", {"rules_version": RULES_VERSION})
    return {"run_id": run_id, "status": sm.GATE_PASSED, "passed": True, "gaps": []}


def run_outline(
    db: Database,
    run: dict,
    brand: dict,
    policy: PolicyYaml,
    providers: dict | None = None,
    key: str | None = None,
    from_file: str | None = None,
) -> dict:
    """Generate (or import) an outline revision.

    ``from_file`` imports outline markdown produced *externally* (e.g. by an
    agent calling this CLI): no LLM provider is invoked, and the audit event
    is marked ``origin: external``. The same structure validation, revision
    counter and approval invalidation apply as for provider-generated
    outlines. Imported content is a legitimate outline change: a fresh file
    creates a new revision and supersedes the previous approval (AC6).
    """
    _resume_allowed(run, "outline")
    if run["status"] not in {sm.GATE_PASSED, sm.OUTLINE_PENDING, sm.APPROVED, sm.DRAFTING, sm.COMPLETED}:
        if run["status"] == sm.BLOCKED:
            raise SeoWriterError(
                "run is blocked; resume via `run retry --step outline` only when the blocker"
                " was approval-related"
            )
        raise GateNotPassedError(
            "research gate must pass before an outline can be generated (`run validate-research`)"
        )
    run_id = run["id"]
    if from_file:
        key = key or idempotency_key(run_id, "outline", {"external": _file_digest(from_file)})
        prior = db.get_command_result(run_id, "outline", key)
        if prior is not None:
            return prior
        content = _read_external(from_file, "outline")
        llm_calls = 0
    else:
        key = key or idempotency_key(run_id, "outline")
        prior = db.get_command_result(run_id, "outline", key)
        if prior is not None:
            return prior
        providers = providers or build_providers(policy)
        llm = llm_provider(providers)
        brief = json.loads(run["brief_snapshot"])
        facts = load_facts(db, brand["id"])
        claims = facts.get("rules", [])
        evidence = db.list_evidence(run_id)
        try:
            result = _call(
                db,
                run_id,
                policy,
                "llm",
                key,
                llm.name,
                llm.profile,
                "llm.outline",
                lambda: llm.generate_outline(brief, evidence, claims),
            )
        except (PermanentProviderError, TransientProviderError) as exc:
            _fail_blocked(db, run, "outline", f"{exc} (retryable={exc.retryable})")
            raise
        content = result.data["markdown"]
        llm_calls = llm.call_count
    structure_errors = validate_outline_structure(content)
    if structure_errors:
        _fail_blocked(db, run, "outline", "; ".join(structure_errors))
        raise ValidationFailedError(structure_errors, step="outline")
    revision = run["outline_revision"] + 1
    db.add_outline(run_id, revision, content)
    db.supersede_approvals(run_id, revision)
    db.set_outline_revision(run_id, revision)
    db.set_approved_revision(run_id, None)
    db.set_status(run_id, sm.OUTLINE_PENDING, step="outline", failure_reason=None)
    origin = {"origin": "external"} if from_file else {}
    db.add_audit(
        run_id, "outline.generated", {"revision": revision, "rules_version": RULES_VERSION, **origin}
    )
    out = {
        "run_id": run_id,
        "status": sm.OUTLINE_PENDING,
        "outline_revision": revision,
        "llm_calls": llm_calls,
    }
    db.record_command(run_id, "outline", key, out)
    return out


def approve_outline(db: Database, run: dict, brand: dict, outline_revision: int, approver: str) -> dict:
    run_id = run["id"]
    if run["status"] not in {sm.OUTLINE_PENDING, sm.APPROVED}:
        raise SeoWriterError(f"approval requires outline_pending_approval (current status: {run['status']})")
    outline = db.get_outline(run_id, outline_revision)
    if outline is None:
        raise NotFoundError(
            f"outline revision {outline_revision} does not exist (latest: {run['outline_revision']})"
        )
    # Approval binds to the *latest* fact snapshot, not the run's creation-time
    # hash: a facts update demotes the run, and re-approval must bind the new
    # facts or the approval would be forever stale. The run keeps its immutable
    # snapshot (manifest) for traceability.
    latest = db.latest_fact_snapshot(brand["id"])
    facts_hash = latest["snapshot_hash"] if latest else run["facts_hash"]
    existing = db.get_approval(run_id, outline_revision)
    if existing and existing["facts_hash"] == facts_hash:
        return {
            "run_id": run_id,
            "status": sm.APPROVED,
            "outline_revision": outline_revision,
            "approver": existing["approver"],
            "idempotent": True,
        }
    db.add_approval(run_id, outline_revision, approver, facts_hash)
    db.supersede_approvals(run_id, outline_revision)
    db.set_approved_revision(run_id, outline_revision)
    db.set_status(run_id, sm.APPROVED, step="outline", failure_reason=None)
    db.add_audit(
        run_id,
        "outline.approved",
        {"outline_revision": outline_revision, "approver": approver, "facts_hash": facts_hash},
    )
    return {
        "run_id": run_id,
        "status": sm.APPROVED,
        "outline_revision": outline_revision,
        "approver": approver,
        "idempotent": False,
    }


def current_approval(db: Database, run: dict, brand: dict) -> dict:
    """The approval that unlocks drafting, or the specific reason it is missing."""
    if run.get("approved_revision") is None:
        raise ApprovalRequiredError("outline is not approved: no approved outline revision")
    current = db.latest_fact_snapshot(brand["id"])
    if current is None:
        raise ApprovalInvalidatedError("brand has no fact snapshot; import facts.yaml and re-approve")
    approval = db.get_approval(run["id"], run["approved_revision"])
    if approval is None:
        raise ApprovalRequiredError(
            f"no explicit approval recorded for outline revision {run['approved_revision']}"
        )
    if approval["facts_hash"] != current["snapshot_hash"]:
        raise ApprovalInvalidatedError("facts changed since approval; re-approve the outline")
    return approval


# ---------------------------------------------------------------------------
# draft + metadata + validation + export
# ---------------------------------------------------------------------------


def run_draft(
    db: Database,
    run: dict,
    brand: dict,
    policy: PolicyYaml,
    providers: dict | None = None,
    key: str | None = None,
    from_file: str | None = None,
) -> dict:
    """Generate (or import) the article draft.

    ``from_file`` imports draft markdown produced *externally* (agent-authored
    copy): no LLM provider is invoked and the audit event is marked
    ``origin: external``. The approval guard runs identically — an
    unapproved or stale approval refuses before anything else, with zero
    provider calls on the refused path (AC4).
    """
    _resume_allowed(run, "draft")
    run_id = run["id"]
    approval = current_approval(db, run, brand)  # raises before any LLM call
    if from_file:
        key = key or idempotency_key(run_id, "draft", {"external": _file_digest(from_file)})
        prior = db.get_command_result(run_id, "draft", key)
        if prior is not None:
            return prior
        content = _read_external(from_file, "draft")
        llm_calls = 0
    else:
        key = key or idempotency_key(run_id, "draft")
        prior = db.get_command_result(run_id, "draft", key)
        if prior is not None:
            return prior
        providers = providers or build_providers(policy)
        llm = llm_provider(providers)
        outline = db.get_outline(run_id, run["approved_revision"])
        facts = load_facts(db, brand["id"])
        try:
            result = _call(
                db,
                run_id,
                policy,
                "llm",
                key,
                llm.name,
                llm.profile,
                "llm.draft",
                lambda: llm.generate_draft(outline["content"], facts.get("rules", [])),
            )
        except (PermanentProviderError, TransientProviderError) as exc:
            _fail_blocked(db, run, "draft", f"{exc} (retryable={exc.retryable})")
            raise
        content = result.data["markdown"]
        llm_calls = llm.call_count
    db.save_draft(run_id, run["approved_revision"], content, {})
    db.set_status(run_id, sm.DRAFTING, step="draft", failure_reason=None)
    origin = {"origin": "external"} if from_file else {}
    db.add_audit(
        run_id,
        "draft.generated",
        {"outline_revision": run["approved_revision"], "approval_id": approval["id"], **origin},
    )
    out = {
        "run_id": run_id,
        "status": sm.DRAFTING,
        "outline_revision": run["approved_revision"],
        "llm_calls": llm_calls,
    }
    db.record_command(run_id, "draft", key, out)
    return out


def run_metadata(
    db: Database,
    run: dict,
    brand: dict,
    policy: PolicyYaml,
    providers: dict | None = None,
    key: str | None = None,
    from_file: str | None = None,
) -> dict:
    """Generate (or import) SEO metadata.

    ``from_file`` imports a YAML metadata document produced *externally*:
    no LLM provider is invoked, the same length/slug validation applies,
    and the audit event is marked ``origin: external``.
    """
    _resume_allowed(run, "metadata")
    run_id = run["id"]
    current_approval(db, run, brand)  # approval guard, like run_draft
    draft = db.get_draft(run_id)
    if draft is None:
        raise NotFoundError("no draft yet; run `seo-writer run draft` first")
    if from_file:
        key = key or idempotency_key(run_id, "metadata", {"external": _file_digest(from_file)})
        prior = db.get_command_result(run_id, "metadata", key)
        if prior is not None:
            return prior
        data = _read_external_yaml(from_file, "metadata")
        llm_calls = 0
    else:
        key = key or idempotency_key(run_id, "metadata")
        prior = db.get_command_result(run_id, "metadata", key)
        if prior is not None:
            return prior
        providers = providers or build_providers(policy)
        llm = llm_provider(providers)
        outline = db.get_outline(run_id, run["approved_revision"])
        paa_pool: list[str] = []
        for ev in db.list_evidence(run_id):
            paa_pool += ev.get("details", {}).get("paa", [])
        try:
            result = _call(
                db,
                run_id,
                policy,
                "llm",
                key,
                llm.name,
                llm.profile,
                "llm.metadata",
                lambda: llm.generate_metadata(outline["content"], draft["article"], paa_pool),
            )
        except (PermanentProviderError, TransientProviderError) as exc:
            _fail_blocked(db, run, "metadata", f"{exc} (retryable={exc.retryable})")
            raise
        data = result.data
        llm_calls = llm.call_count
    meta_errors = validate_metadata_lengths(data)
    if meta_errors:
        _fail_blocked(db, run, "metadata", "; ".join(meta_errors))
        raise ValidationFailedError(meta_errors, step="metadata")
    db.save_draft(run_id, run["approved_revision"], draft["article"], data)
    origin = {"origin": "external"} if from_file else {}
    db.add_audit(run_id, "metadata.generated", {"outline_revision": run["approved_revision"], **origin})
    out = {"run_id": run_id, "status": run["status"], "metadata": data, "llm_calls": llm_calls}
    db.record_command(run_id, "metadata", key, out)
    return out


def run_validate(db: Database, run: dict, brand: dict, policy: PolicyYaml) -> dict:
    _resume_allowed(run, "validate")
    run_id = run["id"]
    draft = db.get_draft(run_id)
    outline = db.get_outline(run_id, run.get("approved_revision")) if run.get("approved_revision") else None
    errors: list[str] = []
    if run.get("approved_revision") is None:
        raise ApprovalRequiredError("no approved outline revision; approve the outline before validating")
    if draft is None:
        errors.append("no draft generated")
    if outline and draft:
        facts = load_facts(db, brand["id"])
        rules = facts.get("rules", [])
        material_terms = facts.get("material_terms", [])
        meta = draft.get("metadata") or {}
        metadata_md = (
            f"Meta title: {meta.get('meta_title', '')}\n"
            f"Meta description: {meta.get('meta_description', '')}\n"
            + "\n".join(str(f) for f in meta.get("faq", []))
        )
        errors += validate_run_corpus(
            outline["content"], draft["article"], metadata_md, rules, material_terms
        )
        errors += validate_metadata_lengths(meta)
        errors += [e for e in validate_outline_structure(outline["content"])]
        try:
            current_approval(db, run, brand)
        except (ApprovalRequiredError, ApprovalInvalidatedError) as exc:
            errors.append(str(exc))
    if errors:
        db.set_status(run_id, sm.BLOCKED, step="validate", failure_reason="; ".join(errors))
        db.add_audit(run_id, "validation.failed", {"errors": errors, "rules_version": RULES_VERSION})
        raise ValidationFailedError(errors, step="validate")
    if run["status"] == sm.DRAFTING:
        db.set_status(run_id, sm.COMPLETED, step="validate", failure_reason=None)
        db.add_audit(run_id, "validation.passed", {"rules_version": RULES_VERSION})
    return {
        "run_id": run_id,
        "status": sm.COMPLETED if run["status"] == sm.DRAFTING else run["status"],
        "passed": True,
        "checks": [],
    }


def run_export(
    ws: Workspace,
    db: Database,
    run: dict,
    brand: dict,
    fmt: str,
    key: str | None = None,
    out_dir: str | None = None,
) -> dict:
    if fmt != "markdown":
        raise UsageError(f"unsupported export format '{fmt}' (Phase 1 supports 'markdown')")
    if run["status"] not in {sm.COMPLETED, sm.EXPORTED}:
        raise SeoWriterError(
            f"export requires status completed (current: {run['status']});"
            " run `seo-writer run validate` first"
        )
    run_id = run["id"]
    key = key or idempotency_key(run_id, "export", {"format": fmt})
    prior = db.get_command_result(run_id, "export", key)
    if prior is not None:
        # Short-circuit skips providers/costs/audits, but a requested out_dir
        # copy is local idempotent I/O: re-materialize it so `--out-dir` on an
        # already-exported run still lands the files where the user asked.
        if out_dir:
            _copy_export_to(prior["article"], prior["manifest"], out_dir)
        return prior
    draft = db.get_draft(run_id)
    outline = db.get_outline(run_id, run["approved_revision"])
    meta = draft["metadata"] or {}
    article = draft["article"] + "\n\n---\n## SEO Metadata\n\n"
    article += f"**Meta Title:** {meta.get('meta_title', '')}\n"
    article += f"**Meta Description:** {meta.get('meta_description', '')}\n"
    article += f"**URL Slug:** {meta.get('slug', '')}\n\n"
    if meta.get("faq"):
        article += (
            "**FAQ Schema:**\n```json\n"
            + json.dumps(
                {
                    "@context": "https://schema.org",
                    "@type": "FAQPage",
                    "mainEntity": [
                        {
                            "@type": "Question",
                            "name": f["q"],
                            "acceptedAnswer": {"@type": "Answer", "text": f["a"]},
                        }
                        for f in meta["faq"]
                    ],
                },
                indent=2,
            )
            + "\n```\n"
        )
    approval = db.get_approval(run_id, run["approved_revision"])
    evidence = db.list_evidence(run_id)
    db.add_audit(
        run_id,
        "export.created",
        {"format": fmt, "rules_version": RULES_VERSION},
    )
    manifest = {
        "manifest_version": 1,
        "rules_version": RULES_VERSION,
        "run_id": run_id,
        "brand": brand["slug"],
        "status": run["status"],
        "brief": json.loads(run["brief_snapshot"]),
        "facts": {"hash": run["facts_hash"], "version": run["facts_version"]},
        "outline": {
            "revision": run["outline_revision"],
            "approved_revision": run["approved_revision"],
            "content_hash": sha256_text(outline["content"]),
        },
        "approval": {
            "outline_revision": approval["outline_revision"],
            "approver": approval["approver"],
            "created_at": approval["created_at"],
            "facts_hash": approval["facts_hash"],
        },
        "evidence": [
            {
                "evidence_id": e["evidence_id"],
                "source_type": e["source_type"],
                "fetch_method": e["fetch_method"],
                "opened_current_run": bool(e["opened_current_run"]),
                "evidence_origin": e["evidence_origin"],
                "url": e["url"],
            }
            for e in evidence
        ],
        "cost_total": db.cost_total(run_id),
        "audit_events": [
            {"event_type": a["event_type"], "payload": a["payload"], "created_at": a["created_at"]}
            for a in db.list_audit(run_id)
        ],
    }
    base = ws.run_dir(run_id) / "export" / fmt
    base.mkdir(parents=True, exist_ok=True)
    article_path = base / "article.md"
    article_path.write_text(article, encoding="utf-8")
    manifest_path = base / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    ws.write_json(run_id, "metadata.json", meta)
    if out_dir:
        _copy_export_to(str(article_path), str(manifest_path), out_dir)
    db.set_status(run_id, sm.EXPORTED, step="export", failure_reason=None)
    out = {
        "run_id": run_id,
        "status": sm.EXPORTED,
        "format": fmt,
        "article": str(article_path),
        "manifest": str(manifest_path),
    }
    db.record_command(run_id, "export", key, out)
    return out


def _file_digest(path: str) -> str:
    """sha256 of the file's bytes — the idempotency input for external imports."""
    return sha256_text(Path(path).expanduser().read_bytes().decode("utf-8"))


def _read_external(path: str, what: str) -> str:
    content = Path(path).expanduser().read_text(encoding="utf-8")
    if not content.strip():
        raise SeoWriterError(f"external {what} file is empty: {path}")
    return content


def _read_external_yaml(path: str, what: str) -> dict[str, Any]:
    content = _read_external(path, what)
    try:
        data = _yaml.safe_load(content)
    except _yaml.YAMLError as exc:
        raise UsageError(f"external {what} file is not valid YAML: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise UsageError(f"external {what} file must contain a YAML mapping: {path}")
    return data


def _copy_export_to(article_path: str, manifest_path: str, out_dir: str) -> None:
    """Copy an export's article + manifest to a caller-specified directory."""
    target = Path(out_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    (target / "article.md").write_text(Path(article_path).read_text(encoding="utf-8"), encoding="utf-8")
    (target / "manifest.json").write_text(Path(manifest_path).read_text(encoding="utf-8"), encoding="utf-8")


def run_retry(
    db: Database, run: dict, brand: dict, step: str, policy: PolicyYaml, providers: dict | None = None
) -> dict:
    """Explicit, non-silent re-execution of a step (fresh idempotency key)."""
    resume = sm.RETRY_RESUME.get(step)
    if resume is None:
        raise UsageError(f"step '{step}' cannot be retried (allowed: {sorted(sm.RETRY_RESUME)})")
    if run["status"] not in resume:
        raise SeoWriterError(f"step '{step}' cannot resume from status '{run['status']}'")
    if step == "research":
        db.set_status(run["id"], sm.RESEARCHING, step="research", failure_reason=None)
        db.add_audit(run["id"], "retry.research", {"from": run["status"]})
        run["status"] = sm.RESEARCHING
        return run_research(db, run, policy, providers=providers, key=f"run:{run['id']}:research:retry")
    if step == "outline":
        db.set_status(run["id"], sm.OUTLINE_PENDING, step="outline", failure_reason=None)
        db.add_audit(run["id"], "retry.outline", {"from": run["status"]})
        run["status"] = sm.OUTLINE_PENDING
        return run_outline(db, run, brand, policy, providers=providers, key=f"run:{run['id']}:outline:retry")
    if step == "draft":
        db.set_status(run["id"], sm.DRAFTING, step="draft", failure_reason=None)
        db.add_audit(run["id"], "retry.draft", {"from": run["status"]})
        run["status"] = sm.DRAFTING
        return run_draft(db, run, brand, policy, providers=providers, key=f"run:{run['id']}:draft:retry")
    raise UsageError(f"step '{step}' has no retry handler")


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


def run_status(db: Database, run: dict) -> dict:
    evidence = db.list_evidence(run["id"])
    counts = {
        "opened_current_run": sum(1 for e in evidence if e["opened_current_run"]),
        "structured_discovery": sum(1 for e in evidence if e["evidence_origin"] == "structured_discovery"),
        "reused_prior_run": sum(1 for e in evidence if e["evidence_origin"] == "reused_prior_run_evidence"),
        "snippet_only": sum(1 for e in evidence if e["fetch_method"] == "snippet_only"),
    }
    return {
        "run_id": run["id"],
        "status": run["status"],
        "step": run["step"],
        "failure_reason": run["failure_reason"],
        "outline_revision": run["outline_revision"],
        "approved_revision": run["approved_revision"],
        "facts_hash": run["facts_hash"],
        "facts_version": run["facts_version"],
        "evidence_counts": counts,
        "cost_total": db.cost_total(run["id"]),
        "approvals": [
            {
                "outline_revision": a["outline_revision"],
                "approver": a["approver"],
                "created_at": a["created_at"],
                "superseded_at": a["superseded_at"],
            }
            for a in db.list_approvals(run["id"])
        ],
    }


def run_evidence(db: Database, run: dict) -> dict:
    evidence = db.list_evidence(run["id"])
    return {
        "run_id": run["id"],
        "count": len(evidence),
        "counts": {
            "opened_current_run": sum(1 for e in evidence if e["opened_current_run"]),
            "structured_discovery": sum(
                1 for e in evidence if e["evidence_origin"] == "structured_discovery"
            ),
            "reused_prior_run": sum(
                1 for e in evidence if e["evidence_origin"] == "reused_prior_run_evidence"
            ),
        },
        "evidence": [
            {
                "evidence_id": e["evidence_id"],
                "source_type": e["source_type"],
                "fetch_method": e["fetch_method"],
                "opened_current_run": bool(e["opened_current_run"]),
                "evidence_origin": e["evidence_origin"],
                "platform": e["platform"],
                "url": e["url"],
                "title": e["title"],
                "grade": e["grade"],
            }
            for e in evidence
        ],
    }


def run_costs(db: Database, run: dict) -> dict:
    rows = db.list_costs(run["id"])
    by_provider: dict[str, float] = {}
    for row in rows:
        by_provider[row["provider"]] = round(by_provider.get(row["provider"], 0.0) + row["cost_estimate"], 6)
    return {
        "run_id": run["id"],
        "total": round(db.cost_total(run["id"]), 6),
        "by_provider": by_provider,
        "entries": [
            {
                "id": row["id"],
                "provider": row["provider"],
                "provider_profile": row["provider_profile"],
                "operation": row["operation"],
                "cost_estimate": row["cost_estimate"],
                "token_estimate": row["token_estimate"],
                "request_fingerprint": row["request_fingerprint"],
                "idempotency_key": row["idempotency_key"],
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }
