from __future__ import annotations

import json
from pathlib import Path

import pytest

from seo_writer import services
from seo_writer.errors import ValidationFailedError
from tests.conftest import happy_path, mock_providers, setup_brand


def _content_map(run_id: str) -> dict:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "sample_scope": {"query_count": 3, "domain_count": 3, "opened_page_count": 3},
        "topics": [
            {"topic_id": "TOPIC-01", "label": "Decision criteria"},
            {"topic_id": "TOPIC-02", "label": "Implementation"},
            {"topic_id": "TOPIC-03", "label": "Tradeoffs"},
        ],
        "pages": [
            {
                "evidence_id": f"SERP-0{i}",
                "url": f"https://competitor{i}.example/guide",
                "domain": f"competitor{i}.example",
                "competitor_types": ["search_competitor"],
                "topic_ids": [f"TOPIC-0{i}"],
                "coverage": {
                    "concept_explanation": i,
                    "evidence_refs": [f"SERP-0{i}"],
                    "summary": "Observed in the sampled page.",
                },
                "evidence_refs": [f"SERP-0{i}"],
            }
            for i in range(1, 4)
        ],
        "gaps": [
            {
                "gap_id": "GAP-01",
                "gap_type": "depth",
                "statement": "In this sample, decision support is incomplete.",
                "buyer_evidence_refs": ["Q-01"],
                "competitor_evidence_refs": ["SERP-01", "SERP-02"],
                "reason_codes": ["DEPTH_NO_DECISION_CRITERIA"],
            }
        ],
        "opportunities": [
            {
                "opportunity_id": f"OPP-0{i}",
                "title": f"Decision criteria guide {i}",
                "gap_types": ["depth"],
                "buyer_need": {"status": "confirmed", "evidence_refs": ["Q-01"]},
                "market_gap": {
                    "confidence": "strong",
                    "reason_codes": ["DEPTH_NO_DECISION_CRITERIA"],
                    "evidence_refs": ["SERP-01", "SERP-02"],
                },
                "brand_fit": {"level": "strong", "fab_refs": ["FAB-F-01"]},
                "differentiation_readiness": {"status": "customer_input_needed"},
                "recommended_format": "comparison_table_plus_guide",
            }
            for i in range(1, 4)
        ],
    }


def test_gap_map_rejects_unknown_evidence_and_invalidates_approval(ws, db, tmp_path) -> None:
    setup_brand(db)
    ctx = happy_path(ws, db, tmp_path, providers=mock_providers())
    run, brand = ctx["run"], ctx["brand"]
    services.approve_outline(db, run, brand, 1, "reviewer")

    bad = _content_map(run["id"])
    bad["gaps"][0]["competitor_evidence_refs"] = ["SERP-404"]
    bad_path = tmp_path / "bad-content-map.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValidationFailedError, match="SERP-404"):
        services.import_gap_map(ws, db, services.resolve_run(db, run["id"]), str(bad_path))

    good_path = tmp_path / "content-map.json"
    good_path.write_text(json.dumps(_content_map(run["id"])), encoding="utf-8")
    result = services.import_gap_map(ws, db, services.resolve_run(db, run["id"]), str(good_path))
    assert Path(result["content_map"]).is_file()
    assert services.resolve_run(db, run["id"])["approved_revision"] is None

    rendered = services.render_run_view(ws, db, services.resolve_run(db, run["id"]), brand, "content-map")
    page = Path(rendered["html"]).read_text(encoding="utf-8")
    assert "Coverage heatmap" in page
    assert "Accessible coverage table" in page


def test_quadrant_uses_levels_not_fake_coordinates(ws, db, tmp_path) -> None:
    setup_brand(db)
    ctx = happy_path(ws, db, tmp_path)
    run, brand = ctx["run"], ctx["brand"]
    path = tmp_path / "content-map.json"
    path.write_text(json.dumps(_content_map(run["id"])), encoding="utf-8")
    services.import_gap_map(ws, db, run, str(path))
    rendered = services.render_run_view(ws, db, run, brand, "opportunities")
    page = Path(rendered["html"]).read_text(encoding="utf-8")
    assert "Brand Fit" in page and "Market Gap Confidence" in page
    assert "Prioritize" in page and "Validate" in page and "Reframe" in page and "Defer" in page
    assert "demand score" not in page.lower()
