"""AC8 — re-running a step with its default key short-circuits: no duplicated
provider costs, no duplicated artifacts, no duplicated audit events. Explicit
fresh keys (retry) intentionally re-execute and re-cost."""

from __future__ import annotations

from seo_writer import services
from tests.conftest import mock_providers, setup_brand, write_topic


def test_ac8_research_rerun_is_free_and_identical(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])

    services.run_research(db, run, policy, providers=providers)
    cost1 = db.cost_total(run["id"])
    audits1 = [a for a in db.list_audit(run["id"]) if a["event_type"] == "research.completed"]

    again = services.run_research(db, services.resolve_run(db, run["id"]), policy, providers=providers)
    assert again["evidence_rows"] == 34
    assert db.cost_total(run["id"]) == cost1, "re-run must not add costs"
    assert len(db.list_evidence(run["id"])) == 34, "no duplicated evidence rows"
    audits2 = [a for a in db.list_audit(run["id"]) if a["event_type"] == "research.completed"]
    assert len(audits2) == len(audits1) == 1, "no duplicated audit events"


def test_ac8_outline_and_draft_rerun_single_llm_call(ws, db, tmp_path) -> None:
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
    services.run_metadata(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)

    llm = providers["llm"]
    calls_after = llm.call_count
    cost_after = db.cost_total(run["id"])
    outlines_after = db.list_outlines(run["id"])

    # every rerun uses its default key -> all three short-circuit
    services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    services.run_metadata(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)

    assert llm.call_count == calls_after, "idempotent reruns must not invoke the LLM"
    assert db.cost_total(run["id"]) == cost_after
    assert db.list_outlines(run["id"]) == outlines_after  # no new revision


def test_ac8_retry_with_fresh_key_intentionally_recosts(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])

    services.run_research(db, run, policy, providers=providers)
    cost1 = db.cost_total(run["id"])
    services.run_retry(
        db, services.resolve_run(db, run["id"]), brand, "research", policy, providers=providers
    )
    assert db.cost_total(run["id"]) > cost1, "explicit retry re-executes and re-costs"
    assert len(db.list_evidence(run["id"])) == 34  # replace_run_evidence keeps one set


def test_ac8_cli_research_short_circuit(tmp_path) -> None:
    """CLI-level: second `run research` reports the same result with equal costs."""

    from tests.conftest import GENERIC_PACK, run_json_cli

    data_dir = tmp_path / "data"
    pack = GENERIC_PACK
    for args in [
        ["init"],
        ["brand", "create", "acme"],
        ["project", "create", "acme", "blog"],
        ["brand", "facts", "import", "acme", str(pack / "facts.yaml")],
        ["brand", "policy", "import", "acme", str(pack / "policy.yaml")],
    ]:
        code, _, err = run_json_cli(args, data_dir)
        assert code == 0, err

    code, created, err = run_json_cli(
        ["run", "create", "acme", "blog", "--brief", str(pack / "topics" / "workflow-decisions.yaml")],
        data_dir,
    )
    assert code == 0, err
    run_id = created["run_id"]
    code, _, err = run_json_cli(["run", "research", run_id], data_dir)
    assert code == 0, err
    code, c1, err = run_json_cli(["run", "costs", run_id], data_dir)
    assert code == 0, err
    code, out2, err = run_json_cli(["run", "research", run_id], data_dir)
    assert code == 0, err
    code, c2, err = run_json_cli(["run", "costs", run_id], data_dir)
    assert code == 0, err
    assert out2["evidence_rows"] == 34
    assert c1["total"] == c2["total"]
