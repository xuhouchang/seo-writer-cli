from __future__ import annotations

from seo_writer.renderers.html import render_report


def test_report_is_deterministic_self_contained_accessible_and_escaped() -> None:
    context = {
        "title": "Content <Map>",
        "brand": "North & Tide",
        "workspace": "test",
        "run_id": "run-1",
        "revision": 2,
        "input_hash": "sha256:" + "a" * 64,
        "status": "Input needed",
    }
    sections = [{"heading": "Evidence", "html": "<p>Safe static content</p>"}]
    first = render_report(context, sections, review_seed={"review_type": "outline"})
    second = render_report(context, sections, review_seed={"review_type": "outline"})

    assert first == second
    assert "Content &lt;Map&gt;" in first
    assert "North &amp; Tide" in first
    assert "https://" not in first and "http://" not in first
    assert "@media print" in first
    assert "@media (max-width: 600px)" in first
    assert "<main" in first and 'aria-label="Review stages"' in first
    assert "Download review JSON" in first
    assert "<noscript>" in first


def test_confidential_text_is_never_rendered() -> None:
    html = render_report(
        {
            "title": "Outline",
            "brand": "Brand",
            "workspace": "test",
            "run_id": "run-1",
            "revision": 1,
            "input_hash": "sha256:" + "b" * 64,
            "status": "Review",
        },
        [
            {
                "heading": "Viewpoints",
                "items": [
                    {"statement": "Publishable", "status": "confirmed"},
                    {"statement": "Secret customer result", "status": "confidential"},
                ],
            }
        ],
    )
    assert "Publishable" in html
    assert "Secret customer result" not in html
