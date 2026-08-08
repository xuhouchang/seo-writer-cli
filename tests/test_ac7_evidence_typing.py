"""AC7 — evidence typing is strict: current-run opened / structured discovery /
snippet-only / reused prior-run are distinct and never conflated. Phase 1 has
no reuse path, so reused-prior-run evidence must always be zero."""

from __future__ import annotations

from seo_writer import services
from tests.conftest import mock_providers, setup_brand, write_topic


def test_ac7_strict_origin_typing(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=providers)

    rows = db.list_evidence(run["id"])
    opened = [r for r in rows if r["opened_current_run"]]
    discovered = [r for r in rows if r["evidence_origin"] == "structured_discovery"]

    # exactly the documented counts from the generic mock run
    assert len(rows) == 34
    assert len(opened) == 16
    assert len(discovered) == 18

    # every opened row is current_run origin with a real body fetch method
    for r in opened:
        assert r["evidence_origin"] == "current_run"
        assert r["fetch_method"] in {"mock_webfetch", "mock_reddit"}
        assert r["url"] and r["title"]

    # every structured-discovery row is explicitly NOT opened
    for r in discovered:
        assert not r["opened_current_run"]
        assert r["evidence_origin"] == "structured_discovery"
        assert r["fetch_method"] in {"mock_api", "mock_webfetch", "snippet_only"}

    # query snapshots are observations, never opened pages
    for q in [r for r in rows if r["source_type"] == "search_query"]:
        assert not q["opened_current_run"]
        assert q["evidence_origin"] == "structured_discovery"
        assert q["details"]["query_method"] == "mock_api"
        assert q["details"]["timestamp"] and q["details"]["location_language_device"]
        assert "aio_visible" in q["details"]

    # candidate threads are snippet-level discovery
    for c in [r for r in rows if r["evidence_id"].startswith("THREAD-CAND")]:
        assert not c["opened_current_run"]
        assert c["evidence_origin"] == "structured_discovery"


def test_ac7_status_counts_and_reuse_never_sneaks_in(ws, db, tmp_path) -> None:
    setup_brand(db)
    providers = mock_providers()
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=providers)

    status = services.run_status(db, services.resolve_run(db, run["id"]))
    counts = status["evidence_counts"]
    assert counts["opened_current_run"] == 16
    assert counts["structured_discovery"] == 18
    assert counts["reused_prior_run"] == 0  # Phase 1 has no reuse path

    report = services.run_evidence(db, services.resolve_run(db, run["id"]))
    assert report["counts"]["reused_prior_run"] == 0
    assert report["count"] == 34
