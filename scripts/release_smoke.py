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

        def checked(args: list[str], label: str) -> dict[str, object]:
            result = run_command(launcher, [*common, *args], env=env, timeout=timeout)
            require(result.returncode == 0, result.stderr or f"{label} failed")
            return json.loads(result.stdout)

        project_root = launcher.parent.parent
        pack = project_root / "examples" / "brand-packs" / "generic-acme"
        checked(["brand", "create", "acme", "--name", "Acme Editorial Co."], "brand create")
        checked(["project", "create", "acme", "blog", "--title", "Acme Blog"], "project create")
        checked(["brand", "facts", "import", "acme", str(pack / "facts.yaml")], "facts import")
        checked(["brand", "policy", "import", "acme", str(pack / "policy.yaml")], "policy import")
        created = checked(
            [
                "run",
                "create",
                "acme",
                "blog",
                "--brief",
                str(pack / "topics" / "workflow-decisions.yaml"),
            ],
            "run create",
        )
        run_id = str(created["run_id"])
        checked(["run", "research", run_id], "research")
        checked(["run", "validate-research", run_id], "validate research")
        content_map = {
            "schema_version": 1,
            "run_id": run_id,
            "sample_scope": {"query_count": 3, "domain_count": 3, "opened_page_count": 3},
            "topics": [
                {"topic_id": "TOPIC-01", "label": "Decision criteria"},
                {"topic_id": "TOPIC-02", "label": "Implementation"},
                {"topic_id": "TOPIC-03", "label": "Tradeoffs"},
            ],
            "pages": [
                {
                    "evidence_id": f"SERP-0{index}",
                    "url": f"https://competitor{index}.example/guide",
                    "domain": f"competitor{index}.example",
                    "competitor_types": ["search_competitor"],
                    "topic_ids": [f"TOPIC-0{index}"],
                    "coverage": {
                        "concept_explanation": index,
                        "evidence_refs": [f"SERP-0{index}"],
                        "summary": "Observed in the deterministic sampled page.",
                    },
                    "evidence_refs": [f"SERP-0{index}"],
                }
                for index in range(1, 4)
            ],
            "gaps": [
                {
                    "gap_id": "GAP-01",
                    "gap_type": "depth",
                    "statement": "In this sample, decision support is incomplete.",
                    "buyer_evidence_refs": ["Q-01"],
                    "competitor_evidence_refs": ["SERP-01", "SERP-02"],
                    "reason_codes": ["DEPTH_NO_DECISION_CRITERIA"],
                }
            ],
            "opportunities": [
                {
                    "opportunity_id": f"OPP-0{index}",
                    "title": f"Decision criteria guide {index}",
                    "gap_types": ["depth"],
                    "buyer_need": {"status": "confirmed", "evidence_refs": ["Q-01"]},
                    "market_gap": {
                        "confidence": "strong",
                        "reason_codes": ["DEPTH_NO_DECISION_CRITERIA"],
                        "evidence_refs": ["SERP-01", "SERP-02"],
                    },
                    "brand_fit": {"level": "strong", "fab_refs": ["FAB-F-01"]},
                    "differentiation_readiness": {"status": "customer_input_needed"},
                    "recommended_format": "comparison_table_plus_guide",
                }
                for index in range(1, 4)
            ],
        }
        content_map_path = root / "content-map.json"
        content_map_path.write_text(json.dumps(content_map), encoding="utf-8")
        checked(["run", "gap-map", run_id, "--from-file", str(content_map_path)], "gap map")
        rendered = checked(["run", "render", run_id, "--view", "opportunities"], "render opportunities")
        html_path = Path(str(rendered["html"]))
        review_manifest_path = Path(str(rendered["review_manifest"]))
        require(html_path.is_file(), "opportunity HTML artifact is missing")
        require(review_manifest_path.is_file(), "opportunity review manifest is missing")
        review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
        review = {
            **{
                key: review_manifest[key]
                for key in (
                    "schema_version",
                    "review_type",
                    "workspace",
                    "brand",
                    "project",
                    "article",
                    "run_id",
                    "revision",
                    "content_map_hash",
                    "artifact_hash",
                    "manifest_hash",
                )
            },
            "reviewer": "release-smoke@example.com",
            "reviewed_at": "2026-08-14T10:00:00+08:00",
            "decisions": [
                {
                    "opportunity_id": "OPP-01",
                    "decision": "prioritize",
                    "note": "Use this deterministic smoke-test direction.",
                }
            ],
        }
        review_path = root / "opportunity-review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        imported = checked(
            ["run", "import-opportunity-review", run_id, str(review_path)],
            "import opportunity review",
        )
        require(imported["opportunity_revision"] == 2, "opportunity review did not create revision 2")
        require(Path(str(imported["artifact"])).is_file(), "opportunity revision artifact is missing")

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
            "checks": [
                "version",
                "help",
                "json-success",
                "exit-1",
                "exit-2",
                "workspace-isolation",
                "opportunity-review-artifacts",
                "opportunity-review-cli",
            ],
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
