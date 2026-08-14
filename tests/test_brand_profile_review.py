from __future__ import annotations

import json
from pathlib import Path

import pytest

from seo_writer import onboard, services
from seo_writer.errors import ValidationFailedError
from tests.conftest import setup_brand

FORBIDDEN_BEHAVIORAL = (
    "customer story",
    "failure recollection",
    "contrarian opinion",
    "unique point of view",
    "proof request",
    "practitioner lesson",
)


def test_brand_profile_html_is_english_factual_and_importable(ws, db, tmp_path) -> None:
    setup_brand(db, name="North Tide")
    onboard.save_site(ws, "acme", "https://north-tide.example")
    brand = services.resolve_brand(db, "acme")
    generated = services.generate_brand_profile_review(ws, db, brand)
    html = Path(generated["html"]).read_text(encoding="utf-8")
    for label in (
        "Company and website",
        "Target audience",
        "Primary use case",
        "Features",
        "Advantages",
        "Benefits",
        "Limitations and non-capabilities",
        "Competitor candidates and alternatives",
        "Primary market",
        "Content language",
    ):
        assert label in html
    lower = html.lower()
    assert all(term not in lower for term in FORBIDDEN_BEHAVIORAL)

    source = json.loads(Path(generated["canonical"]).read_text(encoding="utf-8"))
    review = {
        **{
            k: source[k]
            for k in ("schema_version", "review_type", "workspace", "brand", "revision", "input_hash")
        },
        "reviewer": "buyer@example.com",
        "profile": {
            **source["profile"],
            "target_audience": "B2B content leaders",
            "primary_use_case": "Plan evidence-led articles",
            "features": ["Evidence-led content planning"],
            "advantages": ["Local review artifacts"],
            "benefits": ["Reviewable decisions"],
            "limitations_and_non_capabilities": ["No cloud collaboration"],
            "competitor_candidates_and_alternatives": ["Manual spreadsheets"],
        },
    }
    review_path = tmp_path / "brand-profile-review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    imported = services.import_brand_profile_review(ws, db, brand, str(review_path))
    assert imported["status"]["brand_profile_complete"] is True
    assert imported["status"]["content_language"] == "en"

    with pytest.raises(ValidationFailedError, match="stale"):
        services.import_brand_profile_review(ws, db, brand, str(review_path))
