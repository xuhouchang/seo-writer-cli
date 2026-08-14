# ruff: noqa: E501
"""Content-gap and local HTML review workflow artifacts."""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from html import escape
from math import ceil, sqrt
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import RULES_VERSION
from . import state_machine as sm
from .config import Workspace
from .db import Database
from .errors import NotFoundError, UsageError, ValidationFailedError
from .ids import hash_payload, sha256_text, utcnow
from .models import BrandProfileReview, ContentMap, OpportunityReviewEnvelope, ReviewEnvelope
from .renderers.html import render_report


def _json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise UsageError(f"JSON file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"invalid JSON: {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise UsageError(f"JSON root must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _input_hash(payload: Any) -> str:
    return f"sha256:{hash_payload(payload)}"


def gap_dir(ws: Workspace, run_id: str) -> Path:
    return ws.run_dir(run_id) / "gap"


def brand_review_dir(ws: Workspace, brand_slug: str) -> Path:
    return ws.root / "brands" / brand_slug / "reviews"


_PROFILE_LABELS = (
    ("company_name", "Company and website", "Company name"),
    ("website", "Company and website", "Website"),
    ("target_audience", "Target audience", "Who the product is for"),
    ("primary_use_case", "Primary use case", "The main job the customer completes"),
    ("features", "Features", "One factual capability per line"),
    ("advantages", "Advantages", "One supported differentiation per line"),
    ("benefits", "Benefits", "One audience outcome per line"),
    (
        "limitations_and_non_capabilities",
        "Limitations and non-capabilities",
        "One explicit boundary or non-capability per line",
    ),
    (
        "competitor_candidates_and_alternatives",
        "Competitor candidates and alternatives",
        "One candidate or substitute per line",
    ),
    ("primary_market", "Primary market", "Primary geographic market"),
    ("content_language", "Content language", "English is the only supported value in this release"),
)


def _brand_profile_form(profile: dict[str, Any]) -> str:
    groups = []
    for key, label, helper in _PROFILE_LABELS:
        value = profile.get(key, "")
        text = "\n".join(value) if isinstance(value, list) else str(value)
        control = (
            f'<textarea id="{key}" data-review-field="{key}" rows="4">{escape(text)}</textarea>'
            if isinstance(value, list) or key in {"target_audience", "primary_use_case"}
            else f'<input id="{key}" data-review-field="{key}" value="{escape(text, quote=True)}">'
        )
        groups.append(
            f'<div class="field"><label for="{key}">{escape(label)}</label>{control}<small>{escape(helper)}</small></div>'
        )
    return "".join(groups)


def generate_brand_profile_review(
    ws: Workspace, db: Database, brand: dict, out_dir: str | None = None
) -> dict[str, Any]:
    from . import onboard

    directory = brand_review_dir(ws, brand["slug"])
    canonical_path = directory / "brand-profile-review.json"
    previous = _json(canonical_path) if canonical_path.is_file() else None
    if previous:
        profile = previous["profile"]
        revision = previous["revision"]
    else:
        site = onboard.load_site(ws, brand["slug"]) or {}
        facts = db.latest_facts_payload(brand["id"]) or {"rules": []}
        usable = [
            rule.get("allowed_wording") or rule.get("claim", "")
            for rule in facts["rules"]
            if rule.get("decision") == "approved" and rule.get("safety_level") != "blocked"
        ]
        blocked = [rule.get("claim", "") for rule in facts["rules"] if rule.get("safety_level") == "blocked"]
        profile = BrandProfileReview(
            company_name=brand["name"],
            website=site.get("url", "Not stated"),
            features=[item for item in usable if item],
            limitations_and_non_capabilities=[item for item in blocked if item],
            factual_followups=[
                "Confirm the target audience and primary use case.",
                "Confirm factual competitors or alternatives.",
            ],
        ).model_dump(mode="json")
        revision = 1
    core = {
        "schema_version": 1,
        "review_type": "brand_profile",
        "workspace": ws.slug,
        "brand": brand["slug"],
        "revision": revision,
        "profile": profile,
    }
    core["input_hash"] = _input_hash(core)
    _write_json(canonical_path, core)
    seed = {**core, "reviewer": ""}
    html = render_report(
        {
            "title": "Brand profile review",
            "brand": brand["slug"],
            "workspace": ws.slug,
            "run_id": None,
            "revision": revision,
            "input_hash": core["input_hash"],
            "status": "Factual review",
            "rules_version": RULES_VERSION,
        },
        [{"heading": "Factual brand profile", "html": _brand_profile_form(profile)}],
        review_seed=seed,
    )
    html_path = directory / "brand-profile-review.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    schema_path = directory / "brand-profile-review.schema.json"
    _write_json(schema_path, BrandProfileReview.model_json_schema())
    if out_dir:
        target = Path(out_dir).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        for source in (canonical_path, html_path, schema_path):
            (target / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "brand": brand["slug"],
        "revision": revision,
        "input_hash": core["input_hash"],
        "canonical": str(canonical_path),
        "html": str(html_path),
        "schema": str(schema_path),
    }


def import_brand_profile_review(ws: Workspace, db: Database, brand: dict, source: str) -> dict[str, Any]:
    review = _json(Path(source).expanduser())
    canonical_path = brand_review_dir(ws, brand["slug"]) / "brand-profile-review.json"
    if not canonical_path.is_file():
        raise ValidationFailedError(
            ["brand profile review has not been generated"], step="import_brand_profile"
        )
    canonical = _json(canonical_path)
    errors = []
    for field, expected in (
        ("review_type", "brand_profile"),
        ("workspace", ws.slug),
        ("brand", brand["slug"]),
        ("revision", canonical["revision"]),
        ("input_hash", canonical["input_hash"]),
    ):
        if review.get(field) != expected:
            errors.append(f"stale or mismatched {field}: expected {expected!r}, got {review.get(field)!r}")
    try:
        profile = BrandProfileReview.model_validate(review.get("profile", {}))
    except ValidationError as exc:
        errors.append(str(exc))
        profile = None
    if errors:
        raise ValidationFailedError(errors, step="import_brand_profile")
    imported_at = utcnow()
    import_payload = {
        **review,
        "profile": profile.model_dump(mode="json"),
        "reviewed_at": review.get("reviewed_at") or imported_at,
    }
    import_name = imported_at.replace(":", "-") + ".json"
    import_path = _write_json(
        brand_review_dir(ws, brand["slug"]) / "brand-profile-imports" / import_name,
        import_payload,
    )
    next_core = {
        "schema_version": 1,
        "review_type": "brand_profile",
        "workspace": ws.slug,
        "brand": brand["slug"],
        "revision": canonical["revision"] + 1,
        "profile": profile.model_dump(mode="json"),
    }
    next_core["input_hash"] = _input_hash(next_core)
    _write_json(canonical_path, next_core)
    status = {
        "brand_profile_complete": all(
            str(getattr(profile, key)).strip() not in {"", "Not stated", "[]"}
            for key in ("company_name", "website", "target_audience", "primary_use_case")
        ),
        "fab_complete": all((profile.features, profile.advantages, profile.benefits)),
        "target_audience_confirmed": profile.target_audience != "Not stated",
        "known_limitations_count": len(profile.limitations_and_non_capabilities),
        "competitor_candidates_count": len(profile.competitor_candidates_and_alternatives),
        "content_language": profile.content_language,
        "factual_followups": profile.factual_followups,
    }
    db.add_audit(
        None, "brand_profile.review_imported", {"brand": brand["slug"], "revision": canonical["revision"]}
    )
    return {
        "brand": brand["slug"],
        "revision": next_core["revision"],
        "input_hash": next_core["input_hash"],
        "import": str(import_path),
        "status": status,
    }


def outline_dir(ws: Workspace, run_id: str) -> Path:
    return ws.run_dir(run_id) / "outlines"


def review_dir(ws: Workspace, run_id: str) -> Path:
    return ws.run_dir(run_id) / "reviews"


def opportunity_dir(ws: Workspace, run_id: str) -> Path:
    return gap_dir(ws, run_id) / "opportunities"


def _project_article(db: Database, run: dict) -> tuple[str, str]:
    project = db.get_project_by_id(run["project_id"])
    if project is None:
        raise NotFoundError(f"project id {run['project_id']} does not exist")
    brief = json.loads(run["brief_snapshot"])
    return project["slug"], brief["slug"]


def _content_map_payload(ws: Workspace, run_id: str) -> tuple[Path, dict[str, Any], ContentMap]:
    path = gap_dir(ws, run_id) / "content-map.json"
    if not path.is_file():
        raise NotFoundError("content-map.json does not exist; import a gap map first")
    payload = _json(path)
    supplied_hash = payload.get("input_hash")
    unhashed = {key: value for key, value in payload.items() if key != "input_hash"}
    expected_hash = _input_hash(unhashed)
    if supplied_hash != expected_hash:
        raise ValidationFailedError(["content-map.json input_hash does not match its content"], step="render")
    try:
        model = ContentMap.model_validate(payload)
    except ValidationError as exc:
        raise ValidationFailedError([str(exc)], step="render") from exc
    return path, payload, model


def _opportunity_paths(ws: Workspace, run_id: str, revision: int) -> tuple[Path, Path]:
    root = opportunity_dir(ws, run_id)
    return root / f"rev-{revision}.json", root / f"rev-{revision}.manifest.json"


def _opportunity_revisions(ws: Workspace, run_id: str) -> list[int]:
    revisions = []
    for path in opportunity_dir(ws, run_id).glob("rev-*.json"):
        match = re.fullmatch(r"rev-(\d+)\.json", path.name)
        if match:
            revisions.append(int(match.group(1)))
    return sorted(revisions)


def _write_opportunity_revision(
    ws: Workspace,
    db: Database,
    run: dict,
    brand: dict,
    opportunities: list[dict[str, Any]],
    content_map_hash: str,
    revision: int,
    parent_revision: int | None,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    project, article = _project_article(db, run)
    artifact = {
        "schema_version": 1,
        "review_type": "opportunity",
        "workspace": ws.slug,
        "brand": brand["slug"],
        "project": project,
        "article": article,
        "run_id": run["id"],
        "revision": revision,
        "parent_revision": parent_revision,
        "content_map_hash": content_map_hash,
        "opportunities": opportunities,
    }
    artifact["artifact_hash"] = _input_hash(artifact)
    manifest = {
        key: artifact[key]
        for key in (
            "schema_version",
            "review_type",
            "workspace",
            "brand",
            "project",
            "article",
            "run_id",
            "revision",
            "parent_revision",
            "content_map_hash",
            "artifact_hash",
        )
    }
    manifest["manifest_hash"] = _input_hash(manifest)
    artifact_path, manifest_path = _opportunity_paths(ws, run["id"], revision)
    _write_json(artifact_path, artifact)
    _write_json(manifest_path, manifest)
    return artifact_path, manifest_path, artifact, manifest


def _current_opportunity_revision(
    ws: Workspace, db: Database, run: dict, brand: dict, model: ContentMap, content_map_hash: str
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    revisions = _opportunity_revisions(ws, run["id"])
    if revisions:
        latest = revisions[-1]
        artifact_path, manifest_path = _opportunity_paths(ws, run["id"], latest)
        artifact = _json(artifact_path)
        if artifact.get("content_map_hash") == content_map_hash:
            unhashed = {key: value for key, value in artifact.items() if key != "artifact_hash"}
            if artifact.get("artifact_hash") != _input_hash(unhashed):
                raise ValidationFailedError(
                    ["opportunity artifact_hash does not match its content"], step="render"
                )
            if not manifest_path.is_file():
                raise ValidationFailedError(["opportunity review manifest is missing"], step="render")
            manifest = _json(manifest_path)
            manifest_unhashed = {key: value for key, value in manifest.items() if key != "manifest_hash"}
            if manifest.get("manifest_hash") != _input_hash(manifest_unhashed):
                raise ValidationFailedError(
                    ["opportunity manifest_hash does not match its content"], step="render"
                )
            return artifact_path, manifest_path, artifact, manifest
    revision = (revisions[-1] + 1) if revisions else 1
    return _write_opportunity_revision(
        ws,
        db,
        run,
        brand,
        [item.model_dump(mode="json") for item in model.opportunities],
        content_map_hash,
        revision,
        revisions[-1] if revisions else None,
    )


def import_gap_map(ws: Workspace, db: Database, run: dict, source: str) -> dict[str, Any]:
    sm.assert_step_authorized("gap_map", run["status"])
    try:
        model = ContentMap.model_validate(_json(Path(source).expanduser()))
    except ValidationError as exc:
        raise ValidationFailedError([str(exc)], step="gap_map") from exc
    if model.run_id != run["id"]:
        raise ValidationFailedError(
            [f"content map run_id {model.run_id!r} does not match {run['id']!r}"], step="gap_map"
        )
    known = {row["evidence_id"] for row in db.list_evidence(run["id"])}
    refs: set[str] = set()
    for page in model.pages:
        refs.add(page.evidence_id)
        refs.update(page.evidence_refs)
        refs.update(page.coverage.evidence_refs)
    for gap in model.gaps:
        refs.update(gap.buyer_evidence_refs)
        refs.update(gap.competitor_evidence_refs)
    for opportunity in model.opportunities:
        refs.update(opportunity.buyer_need.evidence_refs)
        refs.update(opportunity.market_gap.evidence_refs)
    unknown = sorted(refs - known)
    if unknown:
        raise ValidationFailedError(
            [f"unknown current-run evidence refs: {', '.join(unknown)}"], step="gap_map"
        )
    payload = model.model_dump(mode="json")
    payload["input_hash"] = _input_hash(payload)
    target = _write_json(gap_dir(ws, run["id"]) / "content-map.json", payload)
    if run.get("approved_revision") is not None:
        db.supersede_approvals(run["id"], run["outline_revision"] + 1)
        db.set_approved_revision(run["id"], None)
        db.set_status(run["id"], sm.OUTLINE_PENDING, step="gap_map")
        db.add_audit(run["id"], "approval.invalidated", {"reason": "gap map changed"})
    db.add_audit(
        run["id"], "gap_map.imported", {"input_hash": payload["input_hash"], "gap_count": len(model.gaps)}
    )
    return {"run_id": run["id"], "content_map": str(target), "input_hash": payload["input_hash"]}


def validate_content_map_for_research(db: Database, run: dict) -> list[str]:
    """Validate an existing map additively; absence preserves the established gate order."""
    path = db.path.parent / "objects" / run["id"] / "gap" / "content-map.json"
    if not path.is_file():
        return []
    try:
        payload = _json(path)
        supplied_hash = payload.get("input_hash")
        unhashed = {key: value for key, value in payload.items() if key != "input_hash"}
        if supplied_hash != _input_hash(unhashed):
            return ["content-map.json input_hash does not match its content"]
        model = ContentMap.model_validate(payload)
    except (UsageError, ValidationError) as exc:
        return [f"content-map.json is invalid: {exc}"]
    if model.run_id != run["id"]:
        return [f"content map run_id {model.run_id!r} does not match {run['id']!r}"]
    known = {row["evidence_id"] for row in db.list_evidence(run["id"])}
    refs: set[str] = set()
    for page in model.pages:
        refs.add(page.evidence_id)
        refs.update(page.evidence_refs)
        refs.update(page.coverage.evidence_refs)
    for gap in model.gaps:
        refs.update(gap.buyer_evidence_refs)
        refs.update(gap.competitor_evidence_refs)
    for opportunity in model.opportunities:
        refs.update(opportunity.buyer_need.evidence_refs)
        refs.update(opportunity.market_gap.evidence_refs)
    unknown = sorted(refs - known)
    return [f"unknown current-run evidence refs: {', '.join(unknown)}"] if unknown else []


def _context(ws: Workspace, run: dict, brand: dict, title: str, input_hash: str) -> dict[str, Any]:
    return {
        "title": title,
        "brand": brand["slug"],
        "workspace": ws.slug,
        "run_id": run["id"],
        "revision": run["outline_revision"] or 1,
        "input_hash": input_hash,
        "status": "Review ready",
        "rules_version": RULES_VERSION,
    }


def _heatmap(model: ContentMap) -> str:
    if len(model.pages) < 3 or len(model.topics) < 3:
        return "<p>Coverage visualization is not rendered because fewer than three pages or topics are available.</p>"
    heads = "".join(f'<th scope="col">{escape(t.label)}</th>' for t in model.topics)
    rows = []
    for page in model.pages:
        score = max(
            page.coverage.concept_explanation,
            page.coverage.decision_criteria,
            page.coverage.implementation_detail,
            page.coverage.limitations_tradeoffs,
            page.coverage.first_party_evidence,
        )
        cells = "".join(
            f'<td class="heat heat-{score}">{score}<span class="sr-only"> out of 3</span></td>'
            if topic.topic_id in page.topic_ids
            else '<td class="heat heat-0">0<span class="sr-only"> out of 3</span></td>'
            for topic in model.topics
        )
        rows.append(f'<tr><th scope="row">{escape(page.domain)}</th>{cells}</tr>')
    return (
        '<div class="chart" role="img" aria-label="Coverage heatmap from zero to three. Darker cells indicate stronger observed coverage.">'
        "<h3>Coverage heatmap</h3><p>Scale: 0 not observed, 1 mentioned, 2 explained, 3 decision-ready.</p>"
        '<div class="table-wrap"><table><caption>Accessible coverage table</caption><thead><tr><th>Domain</th>'
        f"{heads}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>"
    )


_LEVEL = {"weak": 0, "unknown": 0, "moderate": 1, "strong": 2}


def _quadrant(model: ContentMap) -> str:
    if len(model.opportunities) < 3:
        return "<p>Opportunity quadrant is not rendered because fewer than three comparable opportunities are available.</p>"
    points = []
    cell_slots: dict[tuple[str, str], int] = {}
    cell_counts = Counter(
        (item.brand_fit.level, item.market_gap.confidence) for item in model.opportunities
    )
    for marker, opportunity in enumerate(model.opportunities, 1):
        cell = (opportunity.brand_fit.level, opportunity.market_gap.confidence)
        slot = cell_slots.get(cell, 0)
        cell_slots[cell] = slot + 1
        columns = max(1, ceil(sqrt(cell_counts[cell])))
        rows = max(1, ceil(cell_counts[cell] / columns))
        spacing_x = min(30.0, 120.0 / max(columns - 1, 1))
        spacing_y = min(26.0, 80.0 / max(rows - 1, 1))
        offset_x = round((slot % columns - (columns - 1) / 2) * spacing_x)
        offset_y = round((slot // columns - (rows - 1) / 2) * spacing_y)
        x = (110, 220, 310)[_LEVEL[opportunity.brand_fit.level]] + offset_x
        y = (300, 190, 90)[_LEVEL[opportunity.market_gap.confidence]] + offset_y
        color = "#1268d3" if opportunity.differentiation_readiness.status == "confirmed" else "#54a9c7"
        points.append(
            f'<g><circle cx="{x}" cy="{y}" r="9" fill="{color}"><title>{escape(opportunity.title)}</title></circle>'
            f'<text x="{x}" y="{y + 3}" text-anchor="middle" font-size="8" fill="#071526">{marker}</text></g>'
        )
    table_rows = "".join(
        f"<tr><th>{escape(o.opportunity_id)}</th><td>{escape(o.title)}</td><td>{escape(o.brand_fit.level.title())}</td>"
        f"<td>{escape(o.market_gap.confidence.title())}</td><td>{escape(o.differentiation_readiness.status.replace('_', ' ').title())}</td></tr>"
        for o in model.opportunities
    )
    return (
        '<div class="chart"><h3>Opportunity 2x2</h3><p id="quadrant-desc">X axis is Brand Fit. Y axis is Market Gap Confidence. Color indicates Differentiation Readiness.</p>'
        '<svg viewBox="0 0 420 390" role="img" aria-labelledby="quadrant-title quadrant-desc"><title id="quadrant-title">Opportunity quadrant</title>'
        '<line x1="50" y1="350" x2="400" y2="350" stroke="#536174"/><line x1="50" y1="350" x2="50" y2="20" stroke="#536174"/>'
        '<line x1="225" y1="20" x2="225" y2="350" stroke="#cdd5df"/><line x1="50" y1="185" x2="400" y2="185" stroke="#cdd5df"/>'
        '<text x="270" y="45">Prioritize</text><text x="75" y="45">Validate</text><text x="270" y="330">Reframe</text><text x="75" y="330">Defer</text>'
        '<text x="180" y="382">Brand Fit</text><text transform="translate(15 260) rotate(-90)">Market Gap Confidence</text>'
        + "".join(points)
        + "</svg><p>Legend: cobalt means confirmed readiness; cyan means input is needed or unknown. Markers are numbered in table order.</p>"
        '<div class="table-wrap"><table><caption>Opportunity quadrant fallback</caption><thead><tr><th>ID</th><th>Opportunity</th><th>Brand fit</th><th>Market gap</th><th>Readiness</th></tr></thead>'
        f"<tbody>{table_rows}</tbody></table></div></div>"
    )


def _opportunity_review_html(opportunities: list[dict[str, Any]]) -> str:
    choices = (
        ("prioritize", "Prioritize"),
        ("validate", "Validate"),
        ("reframe", "Reframe"),
        ("defer", "Defer"),
        ("excluded", "Exclude"),
    )
    cards = []
    for item in opportunities:
        current = item.get("decision", "candidate")
        options = ['<option value="">No change</option>']
        options.extend(
            f'<option value="{value}"{" selected" if current == value else ""}>{label}</option>'
            for value, label in choices
        )
        cards.append(
            '<article class="record" data-decision><div>'
            f'<h3>{escape(item["title"])}</h3>'
            f'<p>{escape(item["opportunity_id"])} · Current decision: {escape(current.replace("_", " ").title())}</p>'
            f'<input type="hidden" data-key="opportunity_id" value="{escape(item["opportunity_id"], quote=True)}">'
            '<div class="field"><label>Editorial decision'
            f'<select data-key="decision">{"".join(options)}</select></label>'
            '<small>Choose only an editorial priority. Research evidence and scores cannot be edited here.</small></div>'
            '<div class="field"><label>Review note<textarea data-key="note" maxlength="2000"></textarea></label>'
            '<small>Optional editorial context. Do not include confidential information.</small></div>'
            '</div><span class="status">Human decision</span></article>'
        )
    reviewer = (
        '<div class="field"><label for="opportunity-reviewer">Reviewer</label>'
        '<input id="opportunity-reviewer" data-review-field="reviewer" autocomplete="email" required>'
        '<small>Use a name or email that can identify this review in the audit trail.</small></div>'
    )
    return reviewer + "".join(cards)


def _outline_sidecar(ws: Workspace, db: Database, run: dict) -> tuple[Path, dict[str, Any]]:
    outline = db.latest_outline(run["id"])
    if outline is None:
        raise NotFoundError("no outline exists for this run")
    path = outline_dir(ws, run["id"]) / f"rev-{outline['revision']}.json"
    if path.is_file():
        return path, _json(path)
    headings = [
        match.group(1).strip()
        for match in re.finditer(r"^#{2,3}\s+(.+?)\s*$", outline["content"], flags=re.MULTILINE)
    ]
    viewpoints = []
    for index, heading in enumerate(headings or ["Article framework"], 1):
        section_id = f"SEC-{index:02d}"
        viewpoint_id = f"VP-{index:02d}"
        viewpoints.append(
            {
                "viewpoint_id": viewpoint_id,
                "section_id": section_id,
                "statement": heading,
                "viewpoint_type": "consensus",
                "status": "editorial_hypothesis",
                "blocking": False,
                "basis": {"evidence_refs": []},
                "contextual_prompts": [
                    {
                        "prompt_id": f"PROMPT-{index:02d}-01",
                        "section_id": section_id,
                        "viewpoint_id": viewpoint_id,
                        "prompt": "Where does this recommendation most often break down in real projects?",
                        "response_type": "experience",
                    },
                    {
                        "prompt_id": f"PROMPT-{index:02d}-02",
                        "section_id": section_id,
                        "viewpoint_id": viewpoint_id,
                        "prompt": "Can you provide an anonymized example that supports or challenges this section?",
                        "response_type": "case",
                    },
                ],
            }
        )
    core = {
        "schema_version": 1,
        "run_id": run["id"],
        "revision": outline["revision"],
        "outline_hash": f"sha256:{sha256_text(outline['content'])}",
        "viewpoints": viewpoints,
    }
    core["input_hash"] = _input_hash(core)
    return path, _write_json(path, core) and core


def _outline_html(sidecar: dict[str, Any]) -> str:
    cards = []
    choices = [
        ("confirm", "Confirm"),
        ("confirm_with_edits", "Confirm with edits"),
        ("reject", "Reject"),
        ("ask_internal_expert", "Ask an internal expert"),
        ("add_example", "Add an example"),
        ("true_but_confidential", "True, but confidential"),
    ]
    options = "".join(f'<option value="{value}">{label}</option>' for value, label in choices)
    for card in sidecar["viewpoints"]:
        prompts = "".join(
            f"<li>{escape(p['prompt'])}<small> Supports {escape(p['section_id'])} and {escape(p['viewpoint_id'])}.</small></li>"
            for p in card["contextual_prompts"]
        )
        cards.append(
            f'<article class="record" data-decision><div><h3>{escape(card["statement"])}</h3><p>{escape(card["section_id"])} | {escape(card["viewpoint_id"])}</p>'
            f'<input type="hidden" data-key="viewpoint_id" value="{escape(card["viewpoint_id"])}"><ul>{prompts}</ul>'
            f'<div class="field"><label>Decision<select data-key="decision">{options}</select></label></div>'
            '<div class="field"><label>Revised statement<textarea data-key="revised_statement"></textarea></label></div>'
            '<div class="field"><label>Supporting example<textarea data-key="supporting_example"></textarea></label></div>'
            '<div class="field"><label>Publishability<select data-key="publishability"><option value="unknown">Unknown</option><option value="public">Public</option><option value="confidential">Confidential</option></select></label></div>'
            '</div><span class="status">Editorial hypothesis</span></article>'
        )
    architecture = "".join(
        f"<li><strong>{escape(card['section_id'])}</strong>: {escape(card['statement'])}. {len(card['contextual_prompts'])} prompt(s).</li>"
        for card in sidecar["viewpoints"]
    )
    return (
        '<div class="chart" role="img" aria-label="Article architecture listed in section order"><h3>Article architecture map</h3>'
        f"<ol>{architecture}</ol></div>" + "".join(cards)
    )


def _evidence_drawer(rows: list[dict[str, Any]]) -> str:
    details = []
    for row in rows:
        url = row.get("url") or "Not available"
        url_html = (
            f'<a href="{escape(url, quote=True)}">{escape(url)}</a>'
            if str(url).startswith(("http://", "https://"))
            else escape(url)
        )
        details.append(
            f"<details><summary>{escape(row['evidence_id'])}: {escape(row.get('title') or row['source_type'])}</summary>"
            f"<p>Source: {url_html}<br>Fetch method: {escape(row['fetch_method'])}<br>"
            f"Origin: {escape(row['evidence_origin'])}</p></details>"
        )
    return '<div class="evidence">' + "".join(details) + "</div>"


def render_run_view(ws: Workspace, db: Database, run: dict, brand: dict, view: str) -> dict[str, Any]:
    sm.assert_step_authorized("render", run["status"])
    normalized = view.replace("_", "-")
    if normalized in {"content-map", "opportunities"}:
        _path, payload, model = _content_map_payload(ws, run["id"])
        if normalized == "content-map":
            body = _heatmap(model)
            sections = [
                {"heading": "Coverage", "html": body},
                {
                    "heading": "Gap hypotheses",
                    "items": [
                        {
                            "title": gap.statement,
                            "status": "editorial_hypothesis",
                            "description": gap.gap_type,
                        }
                        for gap in model.gaps
                    ],
                },
                {"heading": "Evidence provenance", "html": _evidence_drawer(db.list_evidence(run["id"]))},
            ]
            title = "Content map"
            out = gap_dir(ws, run["id"]) / "content-map.html"
        else:
            artifact_path, manifest_path, artifact, manifest = _current_opportunity_revision(
                ws, db, run, brand, model, payload["input_hash"]
            )
            sections = [
                {"heading": "Opportunity map", "html": _quadrant(model)},
                {
                    "heading": "Opportunity cards",
                    "html": _opportunity_review_html(artifact["opportunities"]),
                },
            ]
            title = "Opportunity map"
            out = gap_dir(ws, run["id"]) / "opportunity-map.html"
        context = _context(ws, run, brand, title, payload["input_hash"])
        review_seed = None
        if normalized == "opportunities":
            context["revision"] = manifest["revision"]
            context["download_label"] = "Download opportunity review JSON"
            review_seed = {
                **{
                    key: manifest[key]
                    for key in (
                        "schema_version",
                        "review_type",
                        "workspace",
                        "brand",
                        "project",
                        "article",
                        "run_id",
                        "revision",
                        "content_map_hash",
                        "artifact_hash",
                        "manifest_hash",
                    )
                },
                "reviewer": "",
                "reviewed_at": "",
                "decisions": [],
            }
        html = render_report(context, sections, review_seed=review_seed)
        out.write_text(html, encoding="utf-8")
        result = {"run_id": run["id"], "view": normalized, "html": str(out)}
        if normalized == "opportunities":
            result.update(
                {
                    "opportunity_revision": manifest["revision"],
                    "artifact": str(artifact_path),
                    "review_manifest": str(manifest_path),
                }
            )
        return result
    if normalized == "outline":
        sidecar_path, sidecar = _outline_sidecar(ws, db, run)
        seed = {
            "schema_version": 1,
            "review_type": "outline",
            "workspace": ws.slug,
            "brand": brand["slug"],
            "run_id": run["id"],
            "revision": sidecar["revision"],
            "input_hash": sidecar["input_hash"],
            "reviewer": "",
            "decisions": [],
        }
        html = render_report(
            _context(ws, run, brand, "Outline review", sidecar["input_hash"]),
            [{"heading": "Outline viewpoints", "html": _outline_html(sidecar)}],
            review_seed=seed,
        )
        out = outline_dir(ws, run["id"]) / f"rev-{sidecar['revision']}.html"
        out.write_text(html, encoding="utf-8")
        return {"run_id": run["id"], "view": "outline", "html": str(out), "sidecar": str(sidecar_path)}
    raise UsageError("view must be one of: content-map, opportunities, outline")


def import_opportunity_review(
    ws: Workspace, db: Database, run: dict, brand: dict, source: str
) -> dict[str, Any]:
    sm.assert_step_authorized("import_opportunity_review", run["status"])
    raw = _json(Path(source).expanduser())
    try:
        review = OpportunityReviewEnvelope.model_validate(raw)
    except ValidationError as exc:
        raise ValidationFailedError([str(exc)], step="import_opportunity_review") from exc

    _map_path, content_map, model = _content_map_payload(ws, run["id"])
    artifact_path, manifest_path, artifact, manifest = _current_opportunity_revision(
        ws, db, run, brand, model, content_map["input_hash"]
    )
    expected = {
        key: manifest[key]
        for key in (
            "review_type",
            "workspace",
            "brand",
            "project",
            "article",
            "run_id",
            "revision",
            "content_map_hash",
            "artifact_hash",
            "manifest_hash",
        )
    }
    errors = []
    for field, value in expected.items():
        actual = getattr(review, field)
        if actual != value:
            errors.append(f"stale or mismatched {field}: expected {value!r}, got {actual!r}")
    known = {item["opportunity_id"] for item in artifact["opportunities"]}
    unknown = sorted({item.opportunity_id for item in review.decisions} - known)
    if unknown:
        errors.append(f"unknown opportunity_id: {', '.join(unknown)}")
    if errors:
        raise ValidationFailedError(errors, step="import_opportunity_review")

    imported_at = utcnow()
    reviewed_at = review.reviewed_at or imported_at
    source_path = Path(source).expanduser().resolve()
    review_payload = review.model_dump(mode="json")
    review_payload["reviewed_at"] = reviewed_at
    review_payload["imported_at"] = imported_at
    review_payload["source"] = str(source_path)
    review_payload["source_hash"] = f"sha256:{sha256_text(source_path.read_text(encoding='utf-8'))}"
    review_payload["review_hash"] = _input_hash(review_payload)
    review_path = _write_json(
        review_dir(ws, run["id"]) / f"opportunity-rev-{review.revision}.review.json",
        review_payload,
    )

    decisions = {item.opportunity_id: item for item in review.decisions}
    next_opportunities = deepcopy(artifact["opportunities"])
    for item in next_opportunities:
        decision = decisions.get(item["opportunity_id"])
        if decision is None:
            continue
        item["decision"] = decision.decision
        item["customer_review"] = {
            "decision": decision.decision,
            "note": decision.note,
            "reviewer": review.reviewer,
            "reviewed_at": reviewed_at,
            "review_hash": review_payload["review_hash"],
        }
    new_revision = review.revision + 1
    next_artifact_path, next_manifest_path, _next_artifact, next_manifest = (
        _write_opportunity_revision(
            ws,
            db,
            run,
            brand,
            next_opportunities,
            review.content_map_hash,
            new_revision,
            review.revision,
        )
    )
    change_summary = dict(sorted(Counter(item.decision for item in review.decisions).items()))
    db.add_audit(
        run["id"],
        "opportunity.review_imported",
        {
            "source": str(source_path),
            "source_hash": review_payload["source_hash"],
            "review_hash": review_payload["review_hash"],
            "reviewer": review.reviewer,
            "reviewed_at": reviewed_at,
            "imported_at": imported_at,
            "parent_revision": review.revision,
            "revision": new_revision,
            "content_map_hash": review.content_map_hash,
            "artifact_hash": next_manifest["artifact_hash"],
            "manifest_hash": next_manifest["manifest_hash"],
            "change_summary": change_summary,
        },
    )
    if run.get("approved_revision") is not None:
        db.supersede_approvals(run["id"], run["outline_revision"] + 1)
        db.set_approved_revision(run["id"], None)
        db.set_status(run["id"], sm.OUTLINE_PENDING, step="import_opportunity_review")
        db.add_audit(
            run["id"], "approval.invalidated", {"reason": "opportunity review changed"}
        )
    return {
        "run_id": run["id"],
        "opportunity_revision": new_revision,
        "parent_revision": review.revision,
        "artifact": str(next_artifact_path),
        "review_manifest": str(next_manifest_path),
        "review": str(review_path),
        "review_hash": review_payload["review_hash"],
        "change_summary": change_summary,
    }


def import_outline_review(ws: Workspace, db: Database, run: dict, brand: dict, source: str) -> dict[str, Any]:
    sm.assert_step_authorized("import_review", run["status"])
    raw = _json(Path(source).expanduser())
    try:
        review = ReviewEnvelope.model_validate(raw)
    except ValidationError as exc:
        raise ValidationFailedError([str(exc)], step="import_review") from exc
    errors = []
    expected = {
        "review_type": "outline",
        "workspace": ws.slug,
        "brand": brand["slug"],
        "run_id": run["id"],
        "revision": run["outline_revision"],
    }
    for field, value in expected.items():
        if getattr(review, field) != value:
            errors.append(f"stale or mismatched {field}: expected {value!r}, got {getattr(review, field)!r}")
    sidecar_path = outline_dir(ws, run["id"]) / f"rev-{run['outline_revision']}.json"
    if not sidecar_path.is_file():
        raise ValidationFailedError(
            ["outline sidecar is missing; render outline first"], step="import_review"
        )
    sidecar = _json(sidecar_path)
    if review.input_hash != sidecar["input_hash"]:
        errors.append("stale input_hash for the current outline revision")
    known = {card["viewpoint_id"] for card in sidecar["viewpoints"]}
    for decision in review.decisions:
        if decision.get("viewpoint_id") not in known:
            errors.append(f"unknown viewpoint_id: {decision.get('viewpoint_id')}")
    if errors:
        raise ValidationFailedError(errors, step="import_review")
    imported = review.model_dump(mode="json")
    imported["reviewed_at"] = imported["reviewed_at"] or utcnow()
    imported["review_hash"] = _input_hash(imported)
    review_path = _write_json(
        review_dir(ws, run["id"]) / f"outline-rev-{review.revision}.review.json", imported
    )
    latest = db.latest_outline(run["id"])
    new_revision = run["outline_revision"] + 1
    db.add_outline(run["id"], new_revision, latest["content"])
    db.supersede_approvals(run["id"], new_revision)
    db.set_outline_revision(run["id"], new_revision)
    db.set_approved_revision(run["id"], None)
    db.set_status(run["id"], sm.OUTLINE_PENDING, step="import_review")
    next_sidecar = {**sidecar, "revision": new_revision, "review_hash": imported["review_hash"]}
    decisions = {d.get("viewpoint_id"): d for d in review.decisions}
    for card in next_sidecar["viewpoints"]:
        decision = decisions.get(card["viewpoint_id"])
        if not decision:
            continue
        if decision.get("revised_statement"):
            card["statement"] = decision["revised_statement"]
        if decision.get("decision") == "reject":
            card["status"] = "rejected"
        elif (
            decision.get("publishability") == "confidential"
            or decision.get("decision") == "true_but_confidential"
        ):
            card["status"] = "confidential"
        else:
            card["status"] = "confirmed"
        card["customer_review"] = decision
    next_sidecar.pop("input_hash", None)
    next_sidecar["input_hash"] = _input_hash(next_sidecar)
    _write_json(outline_dir(ws, run["id"]) / f"rev-{new_revision}.json", next_sidecar)
    db.add_audit(
        run["id"],
        "outline.review_imported",
        {"from_revision": review.revision, "revision": new_revision, "review_hash": imported["review_hash"]},
    )
    return {
        "run_id": run["id"],
        "status": sm.OUTLINE_PENDING,
        "outline_revision": new_revision,
        "review": str(review_path),
        "review_hash": imported["review_hash"],
    }


def render_article_html(article_markdown: str, context: dict[str, Any]) -> str:
    """Render a conservative article review. Markdown is canonical; HTML is derived."""
    blocks: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(f"<p>{escape(' '.join(paragraph))}</p>")
            paragraph.clear()

    for line in article_markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            flush()
            blocks.append(f"<h3>{escape(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            flush()
            blocks.append(f"<h2>{escape(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            flush()
            blocks.append(f"<h2>{escape(stripped[2:])}</h2>")
        elif stripped.startswith("- "):
            flush()
            blocks.append(f"<p>• {escape(stripped[2:])}</p>")
        elif not stripped or stripped.startswith("```"):
            flush()
        else:
            paragraph.append(stripped)
    flush()
    return render_report(
        context,
        [{"heading": "Article", "html": f'<article class="article">{"".join(blocks)}</article>'}],
    )
