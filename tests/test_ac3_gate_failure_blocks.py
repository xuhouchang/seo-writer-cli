"""AC3 — when the research gate fails, the run must block (audited) and no
outline may be generated. Gaps are reported, never silently skipped."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml as _yaml

from seo_writer import services
from seo_writer import state_machine as sm
from seo_writer.errors import SeoWriterError, ValidationFailedError
from tests.conftest import (
    FIXTURES_DIR,
    GENERIC_PACK,
    mock_providers,
    run_json_cli,
    setup_brand,
    write_topic,
)


def _insufficient_run(ws, db, tmp_path: Path, fixture_dir: Path) -> tuple[dict, dict, dict]:
    providers = mock_providers(fixture_dir)
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=providers)
    return services.resolve_run(db, run["id"]), brand, policy


def test_ac3_insufficient_community_blocks_with_gaps(ws, db, tmp_path) -> None:
    setup_brand(db)
    run, brand, policy = _insufficient_run(ws, db, tmp_path, FIXTURES_DIR / "community-insufficient")

    with pytest.raises(ValidationFailedError) as excinfo:
        services.validate_research(db, run, policy)
    reasons = " | ".join(excinfo.value.reasons)
    assert "opened non-promotional community threads" in reasons
    assert "subreddits" in reasons
    assert "second community platform" in reasons

    # run is blocked on the research step, audited
    blocked = services.resolve_run(db, run["id"])
    assert blocked["status"] == sm.BLOCKED
    assert blocked["step"] == "research"
    audit = [a for a in db.list_audit(run["id"]) if a["event_type"] == "research_gate.failed"]
    assert len(audit) == 1
    assert json.loads(audit[0]["payload"])["rules_version"]

    # outline generation is refused on a blocked run
    providers = mock_providers(FIXTURES_DIR / "community-insufficient")
    with pytest.raises(SeoWriterError):
        services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)


def test_ac3_few_serp_pages_blocks(ws, db, tmp_path) -> None:
    setup_brand(db)
    run, _brand, policy = _insufficient_run(ws, db, tmp_path, FIXTURES_DIR / "serp-few")
    with pytest.raises(ValidationFailedError) as excinfo:
        services.validate_research(db, run, policy)
    assert any("opened SERP pages" in r for r in excinfo.value.reasons)
    assert services.resolve_run(db, run["id"])["status"] == sm.BLOCKED


def test_ac3_documented_insufficiency_passes(ws, db, tmp_path) -> None:
    """Second platform absent but documented as insufficient -> gate passes."""
    setup_brand(db)
    run, _brand, policy = _insufficient_run(
        ws, db, tmp_path, FIXTURES_DIR / "community-documented-insufficiency"
    )
    out = services.validate_research(db, run, policy)
    assert out["status"] == sm.GATE_PASSED


def test_ac3_gate_floor_enforced_at_load(tmp_path) -> None:
    """Policy files may not weaken the gate below the Skill floor."""
    raw = _yaml.safe_load((GENERIC_PACK / "policy.yaml").read_text(encoding="utf-8"))
    raw["research_gate"] = {"min_opened_threads": 2}  # below floor of 10
    from pydantic import ValidationError

    from seo_writer.models import PolicyYaml

    with pytest.raises(ValidationError):
        PolicyYaml.model_validate(raw)


def test_ac3_cli_gate_failure_exit_codes(tmp_path) -> None:
    """CLI contract: gate failure exits 1, outline on a blocked run exits 1."""
    data_dir = tmp_path / "data"
    raw = _yaml.safe_load((GENERIC_PACK / "facts.yaml").read_text(encoding="utf-8"))
    (tmp_path / "facts.yaml").write_text(_yaml.safe_dump(raw), encoding="utf-8")
    raw_p = _yaml.safe_load((GENERIC_PACK / "policy.yaml").read_text(encoding="utf-8"))
    for p in raw_p["providers"].values():
        p["fixture_dir"] = str(FIXTURES_DIR / "community-insufficient")
    (tmp_path / "policy.yaml").write_text(_yaml.safe_dump(raw_p), encoding="utf-8")

    for args in [
        ["init"],
        ["brand", "create", "acme"],
        ["project", "create", "acme", "blog"],
        ["brand", "facts", "import", "acme", str(tmp_path / "facts.yaml")],
        ["brand", "policy", "import", "acme", str(tmp_path / "policy.yaml")],
    ]:
        code, out, err = run_json_cli(args, data_dir)
        assert code == 0, f"{args}: {err}"

    topic_path = write_topic(tmp_path)
    code, out, err = run_json_cli(["run", "create", "acme", "blog", "--brief", str(topic_path)], data_dir)
    assert code == 0, err
    run_id = out["run_id"]

    code, out, err = run_json_cli(["run", "research", run_id], data_dir)
    assert code == 0, err
    code, out, err = run_json_cli(["run", "validate-research", run_id], data_dir)
    assert code == 1, "gate failure must exit 1"
    assert '"error": "ValidationFailedError"' in err
    assert "reasons" in err
    code, out, err = run_json_cli(["run", "outline", run_id], data_dir)
    assert code == 1, "outline on a gate-blocked run must exit 1"
