from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from seo_writer import services
from seo_writer.errors import ValidationFailedError
from tests.conftest import happy_path, setup_brand
from tests.test_content_gap_workflow import _content_map


def _review_from_manifest(manifest: dict, **overrides) -> dict:
    review = {
        "schema_version": 1,
        "review_type": "opportunity",
        "workspace": manifest["workspace"],
        "brand": manifest["brand"],
        "project": manifest["project"],
        "article": manifest["article"],
        "run_id": manifest["run_id"],
        "revision": manifest["revision"],
        "content_map_hash": manifest["content_map_hash"],
        "artifact_hash": manifest["artifact_hash"],
        "manifest_hash": manifest["manifest_hash"],
        "reviewer": "editor@example.com",
        "reviewed_at": "2026-08-14T10:00:00+08:00",
        "decisions": [
            {
                "opportunity_id": "OPP-01",
                "decision": "prioritize",
                "note": "Prioritize this direction for the next outline.",
            }
        ],
    }
    review.update(overrides)
    return review


def _setup(ws, db, tmp_path):
    setup_brand(db)
    ctx = happy_path(ws, db, tmp_path)
    run, brand = ctx["run"], ctx["brand"]
    content_map_path = tmp_path / "content-map.json"
    content_map_path.write_text(json.dumps(_content_map(run["id"])), encoding="utf-8")
    services.import_gap_map(ws, db, run, str(content_map_path))
    rendered = services.render_run_view(ws, db, run, brand, "opportunities")
    manifest = json.loads(Path(rendered["review_manifest"]).read_text(encoding="utf-8"))
    return ctx, rendered, manifest


def test_opportunity_html_has_typed_edit_and_download_contract(ws, db, tmp_path) -> None:
    _ctx, rendered, manifest = _setup(ws, db, tmp_path)
    page = Path(rendered["html"]).read_text(encoding="utf-8")

    assert manifest["review_type"] == "opportunity"
    assert manifest["project"] == "blog"
    assert manifest["article"] == "workflow-decisions"
    assert manifest["revision"] == 1
    assert manifest["content_map_hash"].startswith("sha256:")
    assert manifest["artifact_hash"].startswith("sha256:")
    assert manifest["manifest_hash"].startswith("sha256:")
    assert 'data-key="opportunity_id"' in page
    assert 'data-key="decision"' in page
    assert 'data-key="note"' in page
    assert 'data-review-field="reviewer"' in page
    assert "Download opportunity review JSON" in page
    assert "read-only in this release" not in page


def test_opportunity_import_creates_audited_revision_without_mutating_research(ws, db, tmp_path) -> None:
    ctx, _rendered, manifest = _setup(ws, db, tmp_path)
    run, brand = ctx["run"], ctx["brand"]
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "approver@example.com")
    source = ws.run_dir(run["id"]) / "gap" / "content-map.json"
    original_research = source.read_bytes()
    review_path = tmp_path / "opportunity-review.json"
    review_path.write_text(json.dumps(_review_from_manifest(manifest)), encoding="utf-8")

    imported = services.import_opportunity_review(
        ws, db, services.resolve_run(db, run["id"]), brand, str(review_path)
    )

    assert imported["opportunity_revision"] == 2
    assert imported["parent_revision"] == 1
    assert source.read_bytes() == original_research
    revision = json.loads(Path(imported["artifact"]).read_text(encoding="utf-8"))
    changed = next(item for item in revision["opportunities"] if item["opportunity_id"] == "OPP-01")
    untouched = next(item for item in revision["opportunities"] if item["opportunity_id"] == "OPP-02")
    assert changed["decision"] == "prioritize"
    assert changed["customer_review"]["note"] == "Prioritize this direction for the next outline."
    assert changed["market_gap"] == _content_map(run["id"])["opportunities"][0]["market_gap"]
    assert untouched["decision"] == "candidate"
    fresh = services.resolve_run(db, run["id"])
    assert fresh["approved_revision"] is None
    assert fresh["status"] == "outline_pending_approval"
    event = [
        row
        for row in db.list_audit(run["id"])
        if row["event_type"] == "opportunity.review_imported"
    ][-1]
    payload = json.loads(event["payload"])
    assert payload["parent_revision"] == 1
    assert payload["revision"] == 2
    assert payload["reviewer"] == "editor@example.com"
    assert payload["reviewed_at"] == "2026-08-14T10:00:00+08:00"
    assert payload["change_summary"] == {"prioritize": 1}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("review_type", "outline"),
        ("workspace", "wrong-workspace"),
        ("brand", "wrong-brand"),
        ("project", "wrong-project"),
        ("article", "wrong-article"),
        ("run_id", "run-wrong"),
        ("revision", 99),
        ("content_map_hash", "sha256:" + "0" * 64),
        ("artifact_hash", "sha256:" + "1" * 64),
        ("manifest_hash", "sha256:" + "2" * 64),
    ],
)
def test_opportunity_import_fails_closed_on_mismatched_envelope(
    ws, db, tmp_path, field: str, value: object
) -> None:
    ctx, _rendered, manifest = _setup(ws, db, tmp_path)
    review = _review_from_manifest(manifest, **{field: value})
    path = tmp_path / f"bad-{field}.json"
    path.write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(ValidationFailedError, match=field):
        services.import_opportunity_review(ws, db, ctx["run"], ctx["brand"], str(path))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda review: review.update({"unexpected": "not allowed"}),
        lambda review: review.update({"reviewed_at": "2026-08-14 without timezone"}),
        lambda review: review["decisions"][0].update({"market_gap": "stronger"}),
        lambda review: review["decisions"][0].update({"opportunity_id": "OPP-404"}),
    ],
)
def test_opportunity_import_rejects_unknown_fields_and_opportunity_ids(
    ws, db, tmp_path, mutation
) -> None:
    ctx, _rendered, manifest = _setup(ws, db, tmp_path)
    review = copy.deepcopy(_review_from_manifest(manifest))
    mutation(review)
    path = tmp_path / "invalid-review.json"
    path.write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(ValidationFailedError):
        services.import_opportunity_review(ws, db, ctx["run"], ctx["brand"], str(path))


def test_opportunity_review_is_stale_after_successful_import(ws, db, tmp_path) -> None:
    ctx, _rendered, manifest = _setup(ws, db, tmp_path)
    review_path = tmp_path / "opportunity-review.json"
    review_path.write_text(json.dumps(_review_from_manifest(manifest)), encoding="utf-8")
    services.import_opportunity_review(ws, db, ctx["run"], ctx["brand"], str(review_path))

    with pytest.raises(ValidationFailedError, match="stale"):
        services.import_opportunity_review(
            ws, db, services.resolve_run(db, ctx["run"]["id"]), ctx["brand"], str(review_path)
        )


def test_import_opportunity_review_cli_subprocess_contract(ws, db, tmp_path) -> None:
    ctx, _rendered, manifest = _setup(ws, db, tmp_path)
    review_path = tmp_path / "opportunity-review.json"
    review_path.write_text(json.dumps(_review_from_manifest(manifest)), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "seo_writer",
        "--data-dir",
        str(ws.data_dir),
        "--workspace",
        ws.slug,
        "--json",
        "run",
        "import-opportunity-review",
        ctx["run"]["id"],
        str(review_path),
    ]

    success = subprocess.run(command, capture_output=True, text=True, check=False)
    assert success.returncode == 0
    assert success.stderr == ""
    assert json.loads(success.stdout)["opportunity_revision"] == 2

    stale = subprocess.run(command, capture_output=True, text=True, check=False)
    assert stale.returncode == 1
    assert stale.stdout == ""
    error = json.loads(stale.stderr)
    assert error["error"] == "ValidationFailedError"
    assert "stale" in error["message"]


def test_opportunity_revision_is_bound_to_approval_and_export_manifest(ws, db, tmp_path) -> None:
    ctx, _rendered, manifest = _setup(ws, db, tmp_path)
    run, brand, policy = ctx["run"], ctx["brand"], ctx["policy"]
    review_path = tmp_path / "opportunity-review.json"
    review_path.write_text(json.dumps(_review_from_manifest(manifest)), encoding="utf-8")
    services.import_opportunity_review(ws, db, run, brand, str(review_path))
    services.approve_outline(db, services.resolve_run(db, run["id"]), brand, 1, "approver")
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy)
    services.run_metadata(db, services.resolve_run(db, run["id"]), brand, policy)
    services.run_validate(db, services.resolve_run(db, run["id"]), brand, policy)
    exported = services.run_export(
        ws, db, services.resolve_run(db, run["id"]), brand, "html"
    )
    export_manifest = json.loads(Path(exported["manifest"]).read_text(encoding="utf-8"))

    assert "opportunity_artifact" in export_manifest["artifacts"]
    assert "opportunity_review_manifest" in export_manifest["artifacts"]
    assert "opportunity_review" in export_manifest["artifacts"]
    approved = [row for row in db.list_audit(run["id"]) if row["event_type"] == "outline.approved"][-1]
    approval_payload = json.loads(approved["payload"])
    assert approval_payload["opportunity_artifact_hash"].startswith("sha256:")
    assert approval_payload["opportunity_manifest_hash"].startswith("sha256:")
    assert approval_payload["opportunity_review_hash"].startswith("sha256:")
