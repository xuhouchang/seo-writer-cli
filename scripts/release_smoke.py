"""Offline release smoke for the Skill shell's CLI subprocess contract."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run_command(
    launcher: Path,
    args: list[str],
    *,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(launcher), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"SEO Writer launcher not found: {launcher}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"SEO Writer launcher timed out after {timeout:g}s") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def smoke(launcher: Path, timeout: float) -> dict[str, object]:
    launcher = launcher.resolve()
    require(launcher.is_file(), f"SEO Writer launcher not found: {launcher}")

    with tempfile.TemporaryDirectory(prefix="seo-writer-release-smoke-") as temp:
        root = Path(temp)
        fake_home = root / "home"
        data_dir = root / "data"
        fake_home.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        common = [
            "--data-dir",
            str(data_dir),
            "--workspace",
            "release-smoke",
            "--json",
        ]

        version = run_command(launcher, ["--version"], env=env, timeout=timeout)
        require(version.returncode == 0, version.stderr or "--version failed")
        require(version.stdout.startswith("seo-writer "), "unexpected --version output")

        help_result = run_command(launcher, ["--help"], env=env, timeout=timeout)
        require(help_result.returncode == 0, help_result.stderr or "--help failed")
        require("Usage:" in help_result.stdout, "help output has no Usage line")

        init = run_command(launcher, [*common, "init"], env=env, timeout=timeout)
        require(init.returncode == 0, init.stderr or "init failed")
        init_payload = json.loads(init.stdout)
        expected_workspace = data_dir / "release-smoke"
        require(init_payload["workspace"] == str(expected_workspace), "wrong workspace in init JSON")
        require((expected_workspace / "seo-writer.db").is_file(), "init did not create database")
        require(not (fake_home / ".seo-writer").exists(), "smoke touched the default user workspace")

        business = run_command(
            launcher,
            [*common, "gsc", "status", "--brand", "missing"],
            env=env,
            timeout=timeout,
        )
        require(business.returncode == 1, "business error did not exit 1")
        require(json.loads(business.stderr)["error"] == "NotFoundError", "invalid business error JSON")

        usage = run_command(launcher, [*common, "--not-a-real-option"], env=env, timeout=timeout)
        require(usage.returncode == 2, "usage error did not exit 2")
        require(json.loads(usage.stderr)["error"] == "UsageError", "invalid usage error JSON")

        return {
            "status": "passed",
            "launcher": str(launcher),
            "version": version.stdout.strip(),
            "checks": ["version", "help", "json-success", "exit-1", "exit-2", "workspace-isolation"],
            "external_requests": 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--launcher",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "bin" / "seo-writer",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    try:
        result = smoke(args.launcher, args.timeout)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
