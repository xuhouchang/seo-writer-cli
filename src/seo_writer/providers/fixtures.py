"""Fixture loading for deterministic mock providers.

A fixture is an optional YAML file per provider kind in a fixture_dir. Absent
fixtures fall back to built-in deterministic defaults, so an offline mock run
works with zero configuration. Fixtures drive both success and failure paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_fixture(fixture_dir: str | None, kind: str) -> dict[str, Any]:
    if not fixture_dir:
        return {}
    path = Path(fixture_dir) / f"{kind}.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"fixture {path} must be a YAML mapping")
    return data


def pop_failures(fixture: dict[str, Any], operation: str, profile: str) -> list[str]:
    """Extract and consume failure declarations for one operation.

    Each entry: {op, kind: transient|permanent, times: N}. Returns kinds the
    provider should raise on this call; 'times' decrements in the fixture copy
    kept by the provider instance.
    """
    failures = fixture.get("failures", [])
    out: list[str] = []
    remaining: list[dict[str, Any]] = []
    for f in failures:
        if f.get("op") != operation:
            remaining.append(f)
            continue
        times = int(f.get("times", 1))
        if times > 1:
            f["times"] = times - 1
            remaining.append(f)
        out.append(f.get("kind", "transient"))
    fixture["failures"] = remaining
    return out


def fingerprint(*parts: Any) -> str:
    import hashlib
    import json

    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
