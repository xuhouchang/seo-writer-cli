"""Curated source-bundle and launcher release checks."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_release_launcher_runs_offline_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/release_smoke.py", "--launcher", "bin/seo-writer"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["external_requests"] == 0
    assert "opportunity-review-artifacts" in payload["checks"]
    assert "opportunity-review-cli" in payload["checks"]


def test_release_bundle_is_curated_and_contains_no_private_data(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/build_release_bundle.py", "--output-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    archive = Path(payload["archive"])
    assert payload["distribution"] == "source-bundle-not-pypi"
    assert archive.is_file()
    assert archive.with_suffix(archive.suffix + ".sha256").is_file()

    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
        manifest_name = next(name for name in names if name.endswith("/RELEASE-MANIFEST.json"))
        manifest = json.load(bundle.extractfile(manifest_name))  # type: ignore[arg-type]

    required = [
        "/bin/seo-writer",
        "/scripts/release_smoke.py",
        "/skills/seo-writer/SKILL.md",
        "/skills/seo-writer-onboarding/SKILL.md",
        "/src/seo_writer/cli/__init__.py",
        "/pyproject.toml",
        "/uv.lock",
        "/LICENSE",
        "/NOTICE",
    ]
    assert all(any(name.endswith(suffix) for name in names) for suffix in required)
    forbidden = (".csv", ".db", ".sqlite", ".sqlite3", ".pt", ".onnx", ".safetensors")
    assert not any(name.lower().endswith(forbidden) or "/.env" in name for name in names)
    assert manifest["customer_data_included"] is False
    assert manifest["credentials_included"] is False
    assert manifest["model_weights_included"] is False


def test_repository_ignores_customer_runtime_artifacts() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("*.db", "*.csv", "*.tsv", ".secrets.yaml", "**/token.json", "**/client.json"):
        assert pattern in ignored


def test_ci_runs_release_bundle_after_python_matrix() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'python-version: ["3.12", "3.13"]' in workflow
    assert "uv lock --check" in workflow
    assert "scripts/release_smoke.py" in workflow
    assert "scripts/build_release_bundle.py" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_ci_uses_supported_gitleaks_action_contract() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "gitleaks/gitleaks-action@v3" in workflow
    assert "args: detect" not in workflow
    assert "pull-requests: read" in workflow
    assert "fetch-depth: 0" in workflow
