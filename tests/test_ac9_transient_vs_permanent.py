"""AC9 — transient provider errors are retried per policy and recoverable;
permanent errors block immediately and are never silently retried."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seo_writer import services
from seo_writer import state_machine as sm
from seo_writer.errors import PermanentProviderError, TransientProviderError
from tests.conftest import FIXTURES_DIR, mock_providers, setup_brand, write_topic


def _research_with_fixtures(ws, db, tmp_path, fixture_dir: Path) -> tuple[dict, dict, dict, dict]:
    providers = mock_providers(fixture_dir)
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    return run, brand, policy, providers


def test_ac9_transient_failure_retried_then_succeeds(ws, db, tmp_path) -> None:
    setup_brand(db)
    run, brand, policy, providers = _research_with_fixtures(
        ws, db, tmp_path, FIXTURES_DIR / "transient-failures"
    )

    out = services.run_research(db, run, policy, providers=providers)
    assert out["status"] == sm.RESEARCHING

    retries = [a for a in db.list_audit(run["id"]) if a["event_type"] == "provider.retry"]
    assert len(retries) == 1
    assert json.loads(retries[0]["payload"])["op"] == "keyword.search_volume"
    assert json.loads(retries[0]["payload"])["attempt"] == 1
    # the retried call still records exactly one cost row for the operation
    ops = [c["operation"] for c in db.list_costs(run["id"])]
    assert ops.count("keyword.search_volume") == 1


def test_ac9_transient_exhausted_blocks_with_audit(ws, db, tmp_path) -> None:
    setup_brand(db)
    run, brand, policy, providers = _research_with_fixtures(
        ws, db, tmp_path, FIXTURES_DIR / "transient-exhausted"
    )

    with pytest.raises(TransientProviderError):
        services.run_research(db, run, policy, providers=providers)

    blocked = services.resolve_run(db, run["id"])
    assert blocked["status"] == sm.BLOCKED
    assert blocked["step"] == "research"
    retries = [a for a in db.list_audit(run["id"]) if a["event_type"] == "provider.retry"]
    assert len(retries) == 2  # policy.retries=2
    assert any(a["event_type"] == "run.blocked" for a in db.list_audit(run["id"]))
    # failed attempts never hit the cost ledger
    assert all(c["operation"] != "keyword.search_volume" for c in db.list_costs(run["id"]))


def test_ac9_transient_blocked_run_recovers_via_retry(ws, db, tmp_path) -> None:
    """After exhaustion, an explicit `run retry --step research` with fresh
    fixtures (no more failures) recovers the run to the gate."""
    setup_brand(db)
    run, brand, policy, providers = _research_with_fixtures(
        ws, db, tmp_path, FIXTURES_DIR / "transient-exhausted"
    )
    with pytest.raises(TransientProviderError):
        services.run_research(db, run, policy, providers=providers)

    ok_providers = mock_providers()  # no failures declared
    fresh = services.resolve_run(db, run["id"])
    out = services.run_retry(db, fresh, brand, "research", policy, providers=ok_providers)
    assert out["status"] == sm.RESEARCHING
    services.validate_research(db, services.resolve_run(db, run["id"]), policy)


def test_ac9_permanent_failure_never_retried(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers(FIXTURES_DIR / "permanent-failures")
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=providers)
    services.validate_research(db, services.resolve_run(db, run["id"]), policy)

    llm = providers["llm"]
    with pytest.raises(PermanentProviderError):
        services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)

    assert llm.call_count == 1, "permanent error must not be retried"
    assert not any(a["event_type"] == "provider.retry" for a in db.list_audit(run["id"]))
    blocked = services.resolve_run(db, run["id"])
    assert blocked["status"] == sm.BLOCKED
    assert blocked["step"] == "outline"
    assert not any(c["operation"] == "llm.outline" for c in db.list_costs(run["id"])), (
        "failed permanent call must not be billed"
    )
    assert any(a["event_type"] == "run.blocked" for a in db.list_audit(run["id"]))
