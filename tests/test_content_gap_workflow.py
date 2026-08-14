from __future__ import annotations

import json
import re
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


def test_quadrant_assigns_unique_deterministic_slots_beyond_five_points(ws, db, tmp_path) -> None:
    setup_brand(db)
    ctx = happy_path(ws, db, tmp_path)
    run, brand = ctx["run"], ctx["brand"]
    content_map = _content_map(run["id"])
    base = content_map["opportunities"][0]
    content_map["opportunities"] = [
        {
            **base,
            "opportunity_id": f"OPP-{index:02d}",
            "title": (
                "A long evidence-backed opportunity title that must remain readable on narrow screens "
                "without inventing a shorter customer claim"
                if index == 1
                else f"Opportunity {index}"
            ),
        }
        for index in range(1, 9)
    ]
    long_domain = "long-subdomain-for-layout-verification." + "segment-" * 12 + "example"
    content_map["pages"][0]["domain"] = long_domain
    evidence = db.list_evidence(run["id"])
    long_url = "https://example.test/" + "evidence-segment/" * 20 + "source"
    next(row for row in evidence if row["evidence_id"] == "SERP-01")["url"] = long_url
    db.replace_run_evidence(run["id"], evidence)
    path = tmp_path / "many-opportunities.json"
    path.write_text(json.dumps(content_map), encoding="utf-8")
    services.import_gap_map(ws, db, run, str(path))

    first = services.render_run_view(ws, db, run, brand, "opportunities")
    first_page = Path(first["html"]).read_text(encoding="utf-8")
    content_view = services.render_run_view(ws, db, run, brand, "content-map")
    content_page = Path(content_view["html"]).read_text(encoding="utf-8")
    second = services.render_run_view(ws, db, run, brand, "opportunities")
    second_page = Path(second["html"]).read_text(encoding="utf-8")
    coordinates = re.findall(r'<circle cx="(\d+)" cy="(\d+)"', first_page)

    assert len(coordinates) == 8
    assert len(set(coordinates)) == 8
    assert first_page == second_page
    assert "A long evidence-backed opportunity title" in first_page
    assert long_domain in content_page
    assert long_url in content_page
    assert "overflow-wrap:anywhere" in content_page
    static_page = re.sub(r"<script\b[^>]*>.*?</script>", "", first_page, flags=re.DOTALL)
    assert "Opportunity 8" in static_page
    assert "Opportunity quadrant fallback" in static_page
    assert "Core content remains readable without JavaScript" in static_page
