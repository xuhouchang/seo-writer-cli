"""AC10 — every export is traceable to its run: run id, input brief snapshot,
facts hash + version, rules version, outline revision + content hash, explicit
approval (who/when/which facts), evidence source ids and cost total."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seo_writer import RULES_VERSION, services
from seo_writer.ids import sha256_text
from tests.conftest import complete_run, mock_providers, setup_brand, write_topic


def test_ac10_manifest_fully_traceable(ws, db, tmp_path) -> None:
    setup_brand(db)
    ctx = complete_run(ws, db, tmp_path, providers=mock_providers())
    run, brand = ctx["run"], ctx["brand"]

    out = services.run_export(ws, db, run, brand, "markdown")
    manifest = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))

    assert manifest["run_id"] == run["id"]
    assert manifest["rules_version"] == RULES_VERSION
    assert manifest["brand"] == "acme"
    assert manifest["brief"]["brand_slug"] == "acme"
    assert manifest["brief"]["seed_keywords"] == ["editing workflow decisions"]
    assert manifest["facts"]["hash"] == run["facts_hash"]
    assert manifest["facts"]["version"] == run["facts_version"]
    assert manifest["outline"]["revision"] == 1
    assert manifest["outline"]["approved_revision"] == 1

    outline = db.get_outline(run["id"], 1)
    assert manifest["outline"]["content_hash"] == sha256_text(outline["content"])

    approval = manifest["approval"]
    assert approval["outline_revision"] == 1
    assert approval["approver"] == "tester"
    assert approval["created_at"]
    assert approval["facts_hash"] == db.latest_fact_snapshot(brand["id"])["snapshot_hash"]

    evidence_ids = {e["evidence_id"] for e in manifest["evidence"]}
    assert len(evidence_ids) == 34
    assert {"SERP-01", "THREAD-01", "Q-01", "KW-01", "THREAD-SP-01"} <= evidence_ids
    serp01 = next(e for e in manifest["evidence"] if e["evidence_id"] == "SERP-01")
    assert serp01["opened_current_run"] is True
    assert serp01["evidence_origin"] == "current_run"

    assert manifest["cost_total"] == db.cost_total(run["id"]) > 0
    events = [a["event_type"] for a in manifest["audit_events"]]
    for expected in [
        "run.created",
        "research.completed",
        "research_gate.passed",
        "outline.generated",
        "outline.approved",
        "draft.generated",
        "metadata.generated",
        "validation.passed",
        "export.created",
    ]:
        assert expected in events, f"missing audit event {expected}"


def test_ac10_article_contains_metadata_block_and_faq_schema(ws, db, tmp_path) -> None:
    setup_brand(db)
    ctx = complete_run(ws, db, tmp_path)
    run, brand = ctx["run"], ctx["brand"]
    out = services.run_export(ws, db, run, brand, "markdown")
    text = Path(out["article"]).read_text(encoding="utf-8")

    assert "## SEO Metadata" in text
    assert "**Meta Title:**" in text
    assert "**Meta Description:**" in text
    assert "**URL Slug:**" in text
    assert "FAQPage" in text
    faq_start = text.index('"@type": "FAQPage"')
    assert text[faq_start:].count('"@type": "Question"') >= 2


def test_ac10_export_requires_completed_and_is_idempotent(ws, db, tmp_path) -> None:

    setup_brand(db)
    ctx = complete_run(ws, db, tmp_path)
    run, brand = ctx["run"], ctx["brand"]

    # first export records exactly one export.created audit
    out1 = services.run_export(ws, db, run, brand, "markdown")
    assert Path(out1["article"]).exists()
    audits_before = len([a for a in db.list_audit(run["id"]) if a["event_type"] == "export.created"])
    assert audits_before == 1
    # idempotent second export: same files, no second export.created audit
    out2 = services.run_export(ws, db, run, brand, "markdown")
    audits_after = len([a for a in db.list_audit(run["id"]) if a["event_type"] == "export.created"])
    assert audits_after == audits_before == 1
    assert Path(out2["article"]).exists()

    # HTML is an additive export format and remains independently idempotent.
    html = services.run_export(ws, db, run, brand, "html")
    assert Path(html["article"]).name == "article.html"

    # out_dir copy lands article + manifest at the requested path
    target = tmp_path / "out"
    services.run_export(ws, db, run, brand, "markdown", out_dir=str(target))
    assert (target / "article.md").exists()
    assert (target / "manifest.json").exists()
    copied = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert copied["run_id"] == run["id"]


def test_ac10_export_blocked_without_completion(ws, db, tmp_path) -> None:
    from seo_writer.errors import SeoWriterError

    setup_brand(db)
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy)

    with pytest.raises(SeoWriterError):
        services.run_export(ws, db, services.resolve_run(db, run["id"]), brand, "markdown")
