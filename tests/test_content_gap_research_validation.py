from __future__ import annotations

import json

import pytest

from seo_writer import services
from seo_writer.errors import ValidationFailedError
from seo_writer.ids import hash_payload
from tests.conftest import mock_providers, setup_brand, write_topic
from tests.test_content_gap_workflow import _content_map


def test_validate_research_checks_existing_gap_map_without_requiring_one(ws, db, tmp_path) -> None:
    setup_brand(db)
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = services.resolve_run(db, created["run_id"])
    brand = services.resolve_brand(db, "acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=mock_providers())

    # The established order remains valid: no map is required for the first gate.
    clean = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    clean_run = services.resolve_run(db, clean["run_id"])
    services.run_research(db, clean_run, policy, providers=mock_providers())
    assert services.validate_research(db, services.resolve_run(db, clean_run["id"]), policy)["passed"]

    content_map = _content_map(run["id"])
    content_map["gaps"][0]["competitor_evidence_refs"] = ["SERP-404"]
    content_map["input_hash"] = f"sha256:{hash_payload(content_map)}"
    target = ws.run_dir(run["id"]) / "gap" / "content-map.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(content_map), encoding="utf-8")

    with pytest.raises(ValidationFailedError, match="SERP-404"):
        services.validate_research(db, services.resolve_run(db, run["id"]), policy)
