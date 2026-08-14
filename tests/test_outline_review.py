from __future__ import annotations

import json
from pathlib import Path

import pytest

from seo_writer import services
from seo_writer.errors import ValidationFailedError
from tests.conftest import happy_path, setup_brand


def test_outline_review_is_bound_to_revision_and_hash(ws, db, tmp_path) -> None:
    setup_brand(db)
    ctx = happy_path(ws, db, tmp_path)
    run, brand = ctx["run"], ctx["brand"]
    rendered = services.render_run_view(ws, db, run, brand, "outline")
    sidecar = json.loads(Path(rendered["sidecar"]).read_text(encoding="utf-8"))
    card = sidecar["viewpoints"][0]
    assert len(card["contextual_prompts"]) <= 2
    assert all(p["section_id"] == card["section_id"] for p in card["contextual_prompts"])

    review = {
        "schema_version": 1,
        "review_type": "outline",
        "workspace": ws.slug,
        "brand": brand["slug"],
        "run_id": run["id"],
        "revision": run["outline_revision"],
        "input_hash": sidecar["input_hash"],
        "reviewer": "editor@example.com",
        "decisions": [
            {
                "viewpoint_id": card["viewpoint_id"],
                "decision": "confirm_with_edits",
                "revised_statement": "A customer-confirmed decision rule.",
                "supporting_example": "Internal detail",
                "publishability": "confidential",
            }
        ],
    }
    review_path = tmp_path / "outline-review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    imported = services.import_outline_review(ws, db, run, brand, str(review_path))
    assert imported["outline_revision"] == 2
    assert services.resolve_run(db, run["id"])["approved_revision"] is None

    with pytest.raises(ValidationFailedError, match="stale"):
        services.import_outline_review(ws, db, services.resolve_run(db, run["id"]), brand, str(review_path))


def test_article_html_excludes_confidential_review_content(ws, db, tmp_path) -> None:
    setup_brand(db)
    ctx = happy_path(ws, db, tmp_path)
    run, brand, policy = ctx["run"], ctx["brand"], ctx["policy"]
    rendered = services.render_run_view(ws, db, run, brand, "outline")
    sidecar = json.loads(Path(rendered["sidecar"]).read_text(encoding="utf-8"))
    review = {
        "review_type": "outline",
        "workspace": ws.slug,
        "brand": brand["slug"],
        "run_id": run["id"],
        "revision": 1,
        "input_hash": sidecar["input_hash"],
        "reviewer": "editor@example.com",
        "decisions": [
            {
                "viewpoint_id": sidecar["viewpoints"][0]["viewpoint_id"],
                "decision": "true_but_confidential",
                "supporting_example": "SECRET-EXAMPLE-42",
                "publishability": "confidential",
            }
        ],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    services.import_outline_review(ws, db, run, brand, str(path))
    fresh = services.resolve_run(db, run["id"])
    services.approve_outline(db, fresh, brand, 2, "reviewer")
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy)
    services.run_metadata(db, services.resolve_run(db, run["id"]), brand, policy)
    services.run_validate(db, services.resolve_run(db, run["id"]), brand, policy)
    exported = services.run_export(ws, db, services.resolve_run(db, run["id"]), brand, "html")
    article = Path(exported["article"]).read_text(encoding="utf-8")
    assert "SECRET-EXAMPLE-42" not in article
    manifest = json.loads(Path(exported["manifest"]).read_text(encoding="utf-8"))
    assert manifest["artifacts"]["article_html"]["sha256"].startswith("sha256:")
