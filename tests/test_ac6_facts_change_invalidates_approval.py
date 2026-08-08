"""AC6 — modifying the approved outline, the brand facts or the policy
automatically invalidates the approval: the run drops to
outline_pending_approval and drafting is refused until re-approval."""

from __future__ import annotations

import pytest
import yaml as _yaml

from seo_writer import services
from seo_writer import state_machine as sm
from seo_writer.errors import ApprovalInvalidatedError, ApprovalRequiredError, ValidationFailedError
from seo_writer.facts import import_facts
from seo_writer.models import FactsYaml, PolicyYaml
from seo_writer.policy import import_policy
from tests.conftest import GENERIC_PACK, complete_run, mock_providers, setup_brand, write_topic


def _facts_v2() -> dict:
    raw = _yaml.safe_load((GENERIC_PACK / "facts.yaml").read_text(encoding="utf-8"))
    raw["facts_version"] = 2
    raw["rules"][0]["allowed_wording"] = "the review pass checks every segment before any output ships"
    return raw


def _approved_drafting_run(ws, db, tmp_path) -> tuple[dict, dict, dict]:
    setup_brand(db)
    providers = mock_providers()
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=providers)
    services.validate_research(db, services.resolve_run(db, run["id"]), policy)
    services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "tester")
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    return services.resolve_run(db, run["id"]), brand, policy


def test_ac6_facts_update_demotes_and_requires_reapproval(ws, db, tmp_path) -> None:
    run, brand, policy = _approved_drafting_run(ws, db, tmp_path)
    assert run["status"] == sm.DRAFTING

    result = import_facts(db, brand, FactsYaml.model_validate(_facts_v2()))
    assert result["approvals_invalidated"] == 1

    fresh = services.resolve_run(db, run["id"])
    assert fresh["status"] == sm.OUTLINE_PENDING
    audits = [a for a in db.list_audit(run["id"]) if a["event_type"] == "approval.invalidated"]
    assert len(audits) == 1

    with pytest.raises(ApprovalInvalidatedError):
        services.run_draft(db, fresh, brand, policy, providers=mock_providers())

    # re-approving the same outline binds the NEW facts hash and unlocks drafting
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "tester")
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy, providers=mock_providers())
    approval = db.get_approval(run["id"], 1)
    assert approval["facts_hash"] == db.latest_fact_snapshot(brand["id"])["snapshot_hash"]


def test_ac6_new_outline_revision_supersedes_approval(ws, db, tmp_path) -> None:
    run, brand, policy = _approved_drafting_run(ws, db, tmp_path)
    approval_v1 = db.get_approval(run["id"], 1)
    assert approval_v1 is not None and approval_v1["superseded_at"] is None

    # re-generating the outline is an explicit change to an approved artifact:
    # a fresh idempotency key forces re-execution (a default-key rerun would
    # short-circuit as idempotent, per AC8)
    services.run_outline(
        db,
        services.resolve_run(db, run["id"]),
        brand,
        policy,
        providers=mock_providers(),
        key="ac6-regen-outline",
    )
    fresh = services.resolve_run(db, run["id"])
    assert fresh["status"] == sm.OUTLINE_PENDING
    assert fresh["outline_revision"] == 2
    assert fresh["approved_revision"] is None

    stale = db.get_approval(run["id"], 1)
    assert stale["superseded_at"] is not None
    with pytest.raises(ApprovalRequiredError):
        services.run_draft(db, fresh, brand, policy, providers=mock_providers())


def test_ac6_policy_update_demotes_run(ws, db, tmp_path) -> None:
    run, brand, _policy = _approved_drafting_run(ws, db, tmp_path)
    raw = _yaml.safe_load((GENERIC_PACK / "policy.yaml").read_text(encoding="utf-8"))
    raw["retries"] = 3  # changed policy
    result = import_policy(db, brand, PolicyYaml.model_validate(raw))
    assert result["approvals_invalidated"] == 1
    assert services.resolve_run(db, run["id"])["status"] == sm.OUTLINE_PENDING


def test_ac6_stale_approval_blocks_completed_run_validation(ws, db, tmp_path) -> None:
    """Even a completed run's export/validate path refuses after facts change."""
    setup_brand(db)
    ctx = complete_run(ws, db, tmp_path)
    run, brand = ctx["run"], ctx["brand"]
    assert run["status"] == sm.COMPLETED

    import_facts(db, brand, FactsYaml.model_validate(_facts_v2()))
    fresh = services.resolve_run(db, run["id"])
    assert fresh["status"] == sm.OUTLINE_PENDING  # demoted

    policy = services.load_policy(db, brand["id"])
    with pytest.raises(ValidationFailedError):
        services.run_validate(db, fresh, brand, policy)
