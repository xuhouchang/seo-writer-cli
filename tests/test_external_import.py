"""Agent-authored content import: the CLI can register externally produced
outline/draft/metadata files with ZERO LLM provider calls, while every
governance rule (gate, approval, claim safety, idempotency, revisioning)
applies exactly as for provider-generated content."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml as _yaml

from seo_writer import services
from seo_writer import state_machine as sm
from seo_writer.errors import ApprovalRequiredError, ValidationFailedError
from tests.conftest import mock_providers, run_json_cli, setup_brand, write_topic

GOOD_OUTLINE = """## Article Title: Editing Workflow Decisions

### Target Keywords:
- editing workflow decisions

### Search Intent: MOFU

### Section 1: Intro
- opening angle
"""

GOOD_DRAFT = """Every editing workflow starts from a real failure: footage that
needs different treatment per segment, no shared review step, and no honest
boundary for when an automated pass is enough.

The fix is not another tool list — it is a decision sequence you apply before
you touch a timeline. Keep a review step after every automated pass, and treat
the first pass as a starting point, not a deliverable.

The sharper takeaway: your workflow fails where the decision logic is missing.
"""

BLOCKED_DRAFT = GOOD_DRAFT + "\nAcme saves every user four hours per video.\n"

GOOD_META = {
    "meta_title": "Editing Workflow Decisions: A Practical Guide",
    "meta_description": "A practical guide to choosing an editing workflow, with decision logic",
    "slug": "editing-workflow-decisions",
    "faq": [
        {
            "q": "Which workflow fits my footage?",
            "a": "Classify each segment, then apply the decision sequence.",
        },
    ],
    "image_alt_texts": ["Workflow decision table"],
}


def _write(tmp_path: Path, name: str, content: str | dict) -> Path:
    p = tmp_path / name
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        p.write_text(_yaml.safe_dump(content), encoding="utf-8")
    return p


def _gated_run(ws, db, tmp_path, providers) -> tuple[dict, dict, dict]:
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=providers)
    services.validate_research(db, services.resolve_run(db, run["id"]), policy)
    return services.resolve_run(db, run["id"]), brand, policy


def test_external_outline_zero_llm_calls_and_registers_revision(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    run, brand, policy = _gated_run(ws, db, tmp_path, providers)
    path = _write(tmp_path, "outline.md", GOOD_OUTLINE)

    out = services.run_outline(db, run, brand, policy, providers=providers, from_file=str(path))
    assert providers["llm"].call_count == 0, "external import must not invoke the LLM"
    assert out["outline_revision"] == 1
    assert out["llm_calls"] == 0

    fresh = services.resolve_run(db, run["id"])
    assert fresh["status"] == sm.OUTLINE_PENDING
    audit = [a for a in db.list_audit(run["id"]) if a["event_type"] == "outline.generated"][0]
    assert json.loads(audit["payload"])["origin"] == "external"
    assert db.get_outline(run["id"], 1)["content"] == GOOD_OUTLINE


def test_external_outline_structure_still_validated(ws, db, tmp_path) -> None:
    setup_brand(db)
    run, brand, policy = _gated_run(ws, db, tmp_path, mock_providers())
    path = _write(tmp_path, "bad-outline.md", "## Nope\n")

    with pytest.raises(ValidationFailedError):
        services.run_outline(db, run, brand, policy, from_file=str(path))
    assert services.resolve_run(db, run["id"])["status"] == sm.BLOCKED


def test_external_draft_refused_without_approval_zero_llm_calls(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    run, brand, policy = _gated_run(ws, db, tmp_path, providers)
    outline_path = _write(tmp_path, "outline.md", GOOD_OUTLINE)
    services.run_outline(db, run, brand, policy, from_file=str(outline_path))
    path = _write(tmp_path, "draft.md", GOOD_DRAFT)

    with pytest.raises(ApprovalRequiredError):
        services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(path))
    assert providers["llm"].call_count == 0, "refused external draft must not invoke the LLM"
    assert db.get_draft(run["id"]) is None


def test_external_draft_after_approval_imports_and_marks_external(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    run, brand, policy = _gated_run(ws, db, tmp_path, providers)
    outline_path = _write(tmp_path, "outline.md", GOOD_OUTLINE)
    services.run_outline(db, run, brand, policy, from_file=str(outline_path))
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "tester")
    draft_path = _write(tmp_path, "draft.md", GOOD_DRAFT)

    out = services.run_draft(
        db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(draft_path)
    )
    assert providers["llm"].call_count == 0
    assert out["status"] == sm.DRAFTING
    assert db.get_draft(run["id"])["article"] == GOOD_DRAFT
    audit = [a for a in db.list_audit(run["id"]) if a["event_type"] == "draft.generated"][0]
    assert json.loads(audit["payload"])["origin"] == "external"


def test_external_draft_with_blocked_claim_fails_validation(ws, db, tmp_path) -> None:
    setup_brand(db)
    run, brand, policy = _gated_run(ws, db, tmp_path, mock_providers())
    outline_path = _write(tmp_path, "outline.md", GOOD_OUTLINE)
    services.run_outline(db, run, brand, policy, from_file=str(outline_path))
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "tester")
    draft_path = _write(tmp_path, "blocked-draft.md", BLOCKED_DRAFT)
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(draft_path))
    meta_path = _write(tmp_path, "meta.yaml", GOOD_META)
    services.run_metadata(db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(meta_path))

    with pytest.raises(ValidationFailedError) as excinfo:
        services.run_validate(db, services.resolve_run(db, run["id"]), brand, policy)
    reasons = " | ".join(excinfo.value.reasons)
    assert "CLAIM-002" in reasons, "agent-authored blocked claim must be caught"
    assert "four hours per video" in reasons
    assert services.resolve_run(db, run["id"])["status"] == sm.BLOCKED


def test_external_metadata_import_and_length_caps(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    run, brand, policy = _gated_run(ws, db, tmp_path, providers)
    outline_path = _write(tmp_path, "outline.md", GOOD_OUTLINE)
    services.run_outline(db, run, brand, policy, from_file=str(outline_path))
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "tester")
    draft_path = _write(tmp_path, "draft.md", GOOD_DRAFT)
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(draft_path))

    meta_path = _write(tmp_path, "meta.yaml", GOOD_META)
    out = services.run_metadata(
        db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(meta_path)
    )
    assert providers["llm"].call_count == 0
    assert out["metadata"]["slug"] == "editing-workflow-decisions"
    audit = [a for a in db.list_audit(run["id"]) if a["event_type"] == "metadata.generated"][0]
    assert json.loads(audit["payload"])["origin"] == "external"

    # oversized title blocks
    bad = dict(GOOD_META, meta_title="x" * 61)
    bad_path = _write(tmp_path, "bad-meta.yaml", bad)
    with pytest.raises(ValidationFailedError):
        services.run_metadata(db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(bad_path))


def test_external_import_idempotent_same_file(ws, db, tmp_path) -> None:
    setup_brand(db)
    run, brand, policy = _gated_run(ws, db, tmp_path, mock_providers())
    path = _write(tmp_path, "outline.md", GOOD_OUTLINE)
    services.run_outline(db, run, brand, policy, from_file=str(path))
    audits_before = len(db.list_audit(run["id"]))

    again = services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(path))
    assert again["outline_revision"] == 1, "same file must short-circuit, not create rev 2"
    assert len(db.list_outlines(run["id"])) == 1
    assert len(db.list_audit(run["id"])) == audits_before, "no duplicated audit events"


def test_external_import_new_file_supersedes_approval(ws, db, tmp_path) -> None:
    setup_brand(db)
    run, brand, policy = _gated_run(ws, db, tmp_path, mock_providers())
    v1 = _write(tmp_path, "outline-v1.md", GOOD_OUTLINE)
    services.run_outline(db, run, brand, policy, from_file=str(v1))
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "tester")
    v2 = _write(tmp_path, "outline-v2.md", GOOD_OUTLINE.replace("MOFU", "TOFU"))

    fresh = services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, from_file=str(v2))
    assert fresh["outline_revision"] == 2
    assert services.resolve_run(db, run["id"])["approved_revision"] is None
    assert db.get_approval(run["id"], 1)["superseded_at"] is not None


def test_external_import_cli_flow(tmp_path) -> None:
    """CLI-level: full external pipeline via --from-file, then export."""
    from seo_writer import RULES_VERSION
    from tests.conftest import GENERIC_PACK

    data_dir = tmp_path / "data"
    for args in [
        ["init"],
        ["brand", "create", "acme"],
        ["project", "create", "acme", "blog"],
        ["brand", "facts", "import", "acme", str(GENERIC_PACK / "facts.yaml")],
        ["brand", "policy", "import", "acme", str(GENERIC_PACK / "policy.yaml")],
    ]:
        code, _, err = run_json_cli(args, data_dir)
        assert code == 0, err

    topic_path = write_topic(tmp_path)
    code, created, err = run_json_cli(["run", "create", "acme", "blog", "--brief", str(topic_path)], data_dir)
    assert code == 0, err
    run_id = created["run_id"]
    code, _, err = run_json_cli(["run", "research", run_id], data_dir)
    assert code == 0, err
    code, _, err = run_json_cli(["run", "validate-research", run_id], data_dir)
    assert code == 0, err

    outline_path = _write(tmp_path, "outline.md", GOOD_OUTLINE)
    draft_path = _write(tmp_path, "draft.md", GOOD_DRAFT)
    meta_path = _write(tmp_path, "meta.yaml", GOOD_META)

    code, out, err = run_json_cli(["run", "outline", run_id, "--from-file", str(outline_path)], data_dir)
    assert code == 0, err
    assert out["llm_calls"] == 0
    code, _, err = run_json_cli(
        ["run", "approve", run_id, "--revision", "1", "--approver", "tester"], data_dir
    )
    assert code == 0, err
    code, out, err = run_json_cli(["run", "draft", run_id, "--from-file", str(draft_path)], data_dir)
    assert code == 0, err
    assert out["llm_calls"] == 0
    code, out, err = run_json_cli(["run", "metadata", run_id, "--from-file", str(meta_path)], data_dir)
    assert code == 0, err
    assert out["llm_calls"] == 0
    code, _, err = run_json_cli(["run", "validate", run_id], data_dir)
    assert code == 0, err
    code, _, err = run_json_cli(["run", "export", run_id, "--format", "markdown"], data_dir)
    assert code == 0, err

    manifest = json.loads(
        (data_dir / "default" / "objects" / run_id / "export" / "markdown" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["rules_version"] == RULES_VERSION
    origins = [a["payload"] for a in manifest["audit_events"] if a["event_type"] == "draft.generated"]
    assert json.loads(origins[0])["origin"] == "external"
