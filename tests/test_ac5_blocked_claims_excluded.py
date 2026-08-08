"""AC5 — claims the brand ledger marks blocked must never reach the draft,
FAQ or metadata, and validation must fail loudly (never degrade to success)
when a provider injects them."""

from __future__ import annotations

import pytest

from seo_writer import services
from seo_writer import state_machine as sm
from seo_writer.errors import ValidationFailedError
from tests.conftest import FIXTURES_DIR, complete_run, happy_path, mock_providers, setup_brand, write_fixture


def test_ac5_baseline_draft_free_of_blocked_claims(ws, db, tmp_path) -> None:
    setup_brand(db)
    ctx = complete_run(ws, db, tmp_path)
    draft = db.get_draft(ctx["run"]["id"])
    corpus = draft["article"] + str(draft["metadata"])
    for banned in ["four hours", "saves every user", "best ", "guarantee", "no human review"]:
        assert banned not in corpus, f"blocked wording leaked: {banned!r}"
    assert "manual review remains the final step for brand-critical output" in corpus
    assert "operators report fewer re-edits" in corpus


def test_ac5_injected_blocked_claim_fails_validation(ws, db, tmp_path) -> None:
    setup_brand(db)
    fixture_dir = FIXTURES_DIR / "llm-blocked-inject"
    ctx = happy_path(ws, db, tmp_path, providers=mock_providers(fixture_dir))
    run, brand, policy = ctx["run"], ctx["brand"], ctx["policy"]
    services.approve_outline(db, run, brand, run["outline_revision"], "tester")
    services.run_draft(
        db, services.resolve_run(db, run["id"]), brand, policy, providers=mock_providers(fixture_dir)
    )

    # draft contains the injected blocked wording + material term
    draft = db.get_draft(run["id"])["article"]
    assert "four hours per video" in draft

    with pytest.raises(ValidationFailedError) as excinfo:
        services.run_validate(db, services.resolve_run(db, run["id"]), brand, policy)
    reasons = " | ".join(excinfo.value.reasons)
    assert "four hours per video" in reasons, reasons
    assert "CLAIM-002" in reasons

    # run is blocked on validate, audited — never silently completed
    blocked = services.resolve_run(db, run["id"])
    assert blocked["status"] == sm.BLOCKED
    assert blocked["step"] == "validate"
    assert any(a["event_type"] == "validation.failed" for a in db.list_audit(run["id"]))


def test_ac5_faq_and_metadata_lengths_validated(ws, db, tmp_path) -> None:
    """Metadata length caps (title/description/slug/alt) are enforced; too-long
    output blocks, it does not truncate silently."""
    setup_brand(db)
    fixture_dir = tmp_path / "meta-too-long"
    write_fixture(
        fixture_dir,
        "llm",
        {
            "meta_title": "A title that is far too long for the sixty character cap and keeps going",
            "slug": "editing workflow decisions with spaces!",
        },
    )
    ctx = happy_path(ws, db, tmp_path, providers=mock_providers(fixture_dir))
    run, brand, policy = ctx["run"], ctx["brand"], ctx["policy"]
    services.approve_outline(db, run, brand, run["outline_revision"], "tester")
    services.run_draft(
        db, services.resolve_run(db, run["id"]), brand, policy, providers=mock_providers(fixture_dir)
    )
    with pytest.raises(ValidationFailedError) as excinfo:
        services.run_metadata(
            db, services.resolve_run(db, run["id"]), brand, policy, providers=mock_providers(fixture_dir)
        )
    reasons = " | ".join(excinfo.value.reasons)
    assert "meta title" in reasons or "slug" in reasons
    fresh = services.resolve_run(db, run["id"])
    assert fresh["status"] == sm.BLOCKED
