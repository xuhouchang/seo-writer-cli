"""Release-level subprocess contract for the Skill shell and local CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_cli(
    *args: str,
    data_dir: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "seo_writer"]
    if data_dir is not None:
        command.extend(["--data-dir", str(data_dir), "--workspace", "release-smoke", "--json"])
    command.extend(args)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def test_release_entrypoint_version_and_help() -> None:
    version = _run_cli("--version")
    assert version.returncode == 0
    assert version.stdout.startswith("seo-writer 0.1.0")
    assert version.stderr == ""

    help_result = _run_cli("--help")
    assert help_result.returncode == 0
    assert "Usage:" in help_result.stdout and "seo_writer" in help_result.stdout
    assert help_result.stderr == ""


def test_release_json_success_uses_only_temporary_workspace(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    data_dir = tmp_path / "data"
    env = os.environ.copy()
    env["HOME"] = str(fake_home)

    result = _run_cli("init", data_dir=data_dir, env=env)

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(data_dir / "release-smoke")
    assert (data_dir / "release-smoke" / "seo-writer.db").is_file()
    assert not (fake_home / ".seo-writer").exists()


def test_release_json_business_error_is_structured(tmp_path: Path) -> None:
    result = _run_cli("gsc", "status", "--brand", "missing", data_dir=tmp_path / "data")

    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"] == "NotFoundError"
    assert "missing" in payload["message"]


def test_release_json_usage_error_is_structured(tmp_path: Path) -> None:
    result = _run_cli("--not-a-real-option", data_dir=tmp_path / "data")

    assert result.returncode == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"] == "UsageError"
    assert "not-a-real-option" in payload["message"]
