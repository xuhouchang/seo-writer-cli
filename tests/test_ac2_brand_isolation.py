"""AC2 — customer A/B data isolation: facts, approvals and runs never leak
across brands, and a facts update for one brand does not touch the other's
approvals."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from seo_writer import services
from seo_writer.models import FactsYaml, PolicyYaml
from tests.conftest import GENERIC_PACK, complete_run, setup_brand, write_topic

FACTS_A = {
    "brand": "acme",
    "facts_version": 1,
    "rules": [
        {
            "claim_id": "CLAIM-001",
            "claim": "Brand A's review pass checks every segment",
            "safety_level": "safe",
            "allowed_wording": "Brand A's review pass checks every segment",
            "decision": "approved",
        },
        {
            "claim_id": "CLAIM-002",
            "claim": "Brand A saves two hours per video",
            "safety_level": "blocked",
            "disallowed_wording": ["two hours"],
            "reason": "unverified",
            "decision": "blocked",
        },
    ],
    "material_terms": [{"term": "two hours", "claim_id": "CLAIM-002"}],
}

FACTS_B = {
    "brand": "bravo",
    "facts_version": 1,
    "rules": [
        {
            "claim_id": "CLAIM-001",
            "claim": "Brand B's engine transcribes all formats",
            "safety_level": "safe",
            "allowed_wording": "Brand B's engine transcribes all formats",
            "decision": "approved",
        }
    ],
    "material_terms": [],
}


def _setup_brands(ws, db, tmp_path: Path):
    brand_a = setup_brand(db, "acme", "Brand A", facts=FACTS_A)
    # bravo needs its own policy copy: import_policy enforces brand match
    policy_b = copy.deepcopy(yaml.safe_load((GENERIC_PACK / "policy.yaml").read_text(encoding="utf-8")))
    policy_b["brand"] = "bravo"
    brand_b = setup_brand(db, "bravo", "Brand B", facts=FACTS_B, policy=policy_b)
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    return brand_a, brand_b, db.get_run(created["run_id"])


def test_ac2_draft_uses_only_own_brand_claims(ws, db, tmp_path) -> None:
    _brand_a, _brand_b, run = _setup_brands(ws, db, tmp_path)
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])

    services.run_research(db, run, policy)
    services.validate_research(db, services.resolve_run(db, run["id"]), policy)
    services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy)
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "tester")
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy)

    draft = db.get_draft(run["id"])["article"]
    assert "Brand A's review pass checks every segment" in draft
    assert "Brand B" not in draft
    assert "two hours" not in draft  # blocked claim stays out


def test_ac2_facts_update_does_not_invalidate_other_brand(ws, db, tmp_path) -> None:
    brand_a, brand_b, run = _setup_brands(ws, db, tmp_path)

    # A's run is approved and drafting
    ctx = complete_run(ws, db, tmp_path)
    assert ctx["run"]["status"] == "completed"

    # Update B's facts: A's approval must remain valid
    facts_b2 = copy.deepcopy(FACTS_B)
    facts_b2["rules"][0]["allowed_wording"] = "Brand B's engine transcribes everything"
    from seo_writer.facts import import_facts

    result = import_facts(db, brand_b, FactsYaml.model_validate(facts_b2))
    assert result["approvals_invalidated"] == 0

    brand_a = db.get_brand("acme")
    run_a = db.get_run(ctx["run"]["id"])
    approval = services.current_approval(db, run_a, brand_a)
    assert approval["facts_hash"] == db.latest_fact_snapshot(brand_a["id"])["snapshot_hash"]

    # B's rules never appear in A's ledger
    a_rules = db.get_fact_rules(brand_a["id"])
    assert all("Brand B" not in r["claim"] for r in a_rules)


def test_ac2_policy_import_for_b_keeps_a_approval(ws, db, tmp_path) -> None:
    brand_a, brand_b, _run = _setup_brands(ws, db, tmp_path)
    ctx = complete_run(ws, db, tmp_path)

    policy_b = PolicyYaml.model_validate(
        yaml.safe_load((GENERIC_PACK / "policy.yaml").read_text(encoding="utf-8"))
    )
    policy_b.brand = "bravo"
    from seo_writer.policy import import_policy

    result = import_policy(db, brand_b, policy_b)
    assert result["approvals_invalidated"] == 0

    # A's completed run still validates (approval intact)
    run_a = db.get_run(ctx["run"]["id"])
    assert run_a["status"] == "completed"
    brand_a = db.get_brand("acme")
    services.current_approval(db, run_a, brand_a)
