"""Shared test scaffolding: isolated workspace, brand setup, topic files.

Every test gets its own tmp data dir so nothing touches ~/.seo-writer and no
run ever crosses brands. The generic example pack is the baseline fixture;
tests override facts/policy by writing their own YAML.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from seo_writer import services
from seo_writer.config import ensure_workspace
from seo_writer.db import Database
from seo_writer.facts import import_facts
from seo_writer.models import FactsYaml, PolicyYaml
from seo_writer.policy import import_policy

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GENERIC_PACK = Path(__file__).parent.parent / "examples" / "brand-packs" / "generic-acme"


@pytest.fixture
def ws(tmp_path: Path):
    return ensure_workspace(tmp_path / "data", "test")


@pytest.fixture
def db(ws):
    return ws.open_db()


def _load(pack_dir: Path, name: str) -> dict:
    return yaml.safe_load((pack_dir / name).read_text(encoding="utf-8"))


def setup_brand(
    db: Database,
    slug: str = "acme",
    name: str = "Acme Test Co.",
    facts: dict | None = None,
    policy: dict | None = None,
) -> dict:
    """Create brand + project and import the given (or generic) facts/policy."""
    brand = db.create_brand(slug, name)
    db.create_project(brand["id"], "blog", "Test Blog")
    facts_model = FactsYaml.model_validate(facts or _load(GENERIC_PACK, "facts.yaml"))
    import_facts(db, brand, facts_model)
    policy_model = PolicyYaml.model_validate(policy or _load(GENERIC_PACK, "policy.yaml"))
    import_policy(db, brand, policy_model)
    return brand


def write_topic(tmp_path: Path, **overrides) -> Path:
    """Write a valid topic.yaml and return its path."""
    topic = {
        "slug": "workflow-decisions",
        "title": "Editing Workflow Decisions: A Practical Guide",
        "seed_keywords": ["editing workflow decisions"],
        "intent": "MOFU",
        "target_word_count": 1500,
        "audience": "operators",
        "existing_blog_urls": [],
        "notes": "",
    }
    topic.update(overrides)
    path = tmp_path / "topic.yaml"
    path.write_text(yaml.safe_dump(topic), encoding="utf-8")
    return path


def mock_providers(fixture_dir: Path | str | None = None) -> dict:
    """Build deterministic mock providers; optional fixture_dir drives overrides."""
    raw = dict(_load(GENERIC_PACK, "policy.yaml"))
    if fixture_dir:
        for p in raw["providers"].values():
            p["fixture_dir"] = str(fixture_dir)
    from seo_writer.providers import build_providers

    return build_providers(PolicyYaml.model_validate(raw))


def write_fixture(dir_path: Path, kind: str, data: dict) -> Path:
    """Write <dir>/<kind>.yaml."""
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{kind}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def happy_path(
    ws,
    db: Database,
    tmp_path: Path,
    providers: dict | None = None,
    **topic_overrides,
) -> dict:
    """create -> research -> gate -> outline; returns fresh run/brand/policy."""
    created = services.create_run(ws, db, "acme", "blog", str(write_topic(tmp_path, **topic_overrides)))
    run = db.get_run(created["run_id"])
    brand = db.get_brand("acme")
    policy = services.load_policy(db, brand["id"])
    services.run_research(db, run, policy, providers=providers)
    services.validate_research(db, services.resolve_run(db, run["id"]), policy)
    services.run_outline(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    return {
        "run": services.resolve_run(db, run["id"]),
        "brand": brand,
        "policy": policy,
        "providers": providers,
    }


def complete_run(ws, db: Database, tmp_path: Path, providers: dict | None = None) -> dict:
    """Full happy path through COMPLETED (outline approved + draft + metadata)."""
    ctx = happy_path(ws, db, tmp_path, providers)
    run, brand, policy = ctx["run"], ctx["brand"], ctx["policy"]
    services.approve_outline(db, run, brand, run["outline_revision"], "tester")
    services.run_draft(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    services.run_metadata(db, services.resolve_run(db, run["id"]), brand, policy, providers=providers)
    services.run_validate(db, services.resolve_run(db, run["id"]), brand, policy)
    ctx["run"] = services.resolve_run(db, run["id"])
    return ctx


def run_json_cli(args: list[str], data_dir: Path) -> tuple[int, dict | None, str]:
    """Run the CLI as a subprocess; returns (exit_code, parsed stdout json, stderr)."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "seo_writer", "--data-dir", str(data_dir), "--json", *args],
        capture_output=True,
        text=True,
    )
    out = None
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
    return proc.returncode, out, proc.stderr
