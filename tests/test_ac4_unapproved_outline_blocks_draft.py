"""AC4 — without an explicit, current approval the draft step must fail
BEFORE any LLM call: zero provider invocations on the refused path."""

from __future__ import annotations

import pytest

from seo_writer import services
from seo_writer.errors import ApprovalRequiredError
from tests.conftest import mock_providers, setup_brand, write_topic


def test_ac4_unapproved_outline_blocks_draft_zero_llm_calls(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])

    services.run_research(db, run, policy, providers=providers)
    services.validate_research(db, services.resolve_run(db, run["id"]), policy)
    services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    llm = providers["llm"]
    calls_after_outline = llm.call_count
    assert calls_after_outline == 1  # exactly one LLM call so far

    with pytest.raises(ApprovalRequiredError):
        services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    assert llm.call_count == calls_after_outline, "draft must not invoke the LLM without approval"

    # no draft artifact, no cost for llm.draft, run stays outline_pending_approval
    assert db.get_draft(run["id"]) is None
    assert db.cost_total(run["id"]) == 0.0 or all(
        c["operation"] != "llm.draft" for c in db.list_costs(run["id"])
    )
    fresh = services.resolve_run(db, run["id"])
    assert fresh["status"] == "outline_pending_approval"


def test_ac4_metadata_and_validate_also_require_approval(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=providers)
    services.validate_research(db, services.resolve_run(db, run["id"]), policy)
    services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)

    with pytest.raises(ApprovalRequiredError):
        services.run_metadata(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    with pytest.raises(ApprovalRequiredError):
        # metadata refused -> no draft -> validate reports missing draft as a
        # validation error rather than silently passing
        services.run_validate(db, services.resolve_run(db, run["id"]), brand, policy)
