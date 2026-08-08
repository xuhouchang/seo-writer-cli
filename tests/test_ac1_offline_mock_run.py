"""AC1 — a complete offline run with the generic example pack must succeed
end-to-end through export, using only deterministic mock providers (no paid
APIs, no network, no secrets)."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import GENERIC_PACK, run_json_cli


def _setup_cli(data_dir: Path) -> None:
    """init + brand + project + facts + policy via the CLI itself."""
    pack = GENERIC_PACK
    for args in [
        ["init"],
        ["brand", "create", "acme", "--name", "Acme Editorial Co."],
        ["project", "create", "acme", "blog", "--title", "Acme Blog"],
        ["brand", "facts", "import", "acme", str(pack / "facts.yaml")],
        ["brand", "policy", "import", "acme", str(pack / "policy.yaml")],
    ]:
        code, out, err = run_json_cli(args, data_dir)
        assert code == 0, f"{args} failed: {err}"


def test_ac1_full_offline_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _setup_cli(data_dir)

    code, created, err = run_json_cli(
        [
            "run",
            "create",
            "acme",
            "blog",
            "--brief",
            str(GENERIC_PACK / "topics" / "workflow-decisions.yaml"),
        ],
        data_dir,
    )
    assert code == 0, err
    assert created["status"] == "created"
    run_id = created["run_id"]

    expected = [
        ("research", "researching"),
        ("validate-research", "research_gate_passed"),
        ("outline", "outline_pending_approval"),
    ]
    for cmd, want_status in expected:
        code, out, err = run_json_cli(["run", cmd, run_id], data_dir)
        assert code == 0, f"{cmd}: {err}"
        assert out["status"] == want_status, f"{cmd}: {out}"

    code, out, err = run_json_cli(
        ["run", "approve", run_id, "--revision", "1", "--approver", "demo"], data_dir
    )
    assert code == 0, err
    assert out["status"] == "approved"

    for cmd, want_status in [("draft", "drafting"), ("metadata", "drafting"), ("validate", "completed")]:
        code, out, err = run_json_cli(["run", cmd, run_id], data_dir)
        assert code == 0, f"{cmd}: {err}"
        assert out["status"] == want_status, f"{cmd}: {out}"

    code, out, err = run_json_cli(["run", "export", run_id], data_dir)
    assert code == 0, err
    assert out["status"] == "exported"

    article = Path(out["article"])
    manifest = Path(out["manifest"])
    assert article.exists() and manifest.exists()
    text = article.read_text(encoding="utf-8")
    assert "## SEO Metadata" in text
    assert "FAQPage" in text  # JSON-LD block embedded
    assert "editing workflow decisions" in text.lower()  # primary keyword present


def test_ac1_zero_secrets_in_artifacts(tmp_path: Path) -> None:
    """No API keys, paths or customer data leak into exports or the db file."""
    data_dir = tmp_path / "data"
    _setup_cli(data_dir)
    code, created, err = run_json_cli(
        [
            "run",
            "create",
            "acme",
            "blog",
            "--brief",
            str(GENERIC_PACK / "topics" / "workflow-decisions.yaml"),
        ],
        data_dir,
    )
    assert code == 0, err
    run_id = created["run_id"]
    for cmd in [
        "research",
        "validate-research",
        "outline",
        "approve",
        "draft",
        "metadata",
        "validate",
        "export",
    ]:
        args = ["run", cmd, run_id]
        if cmd == "approve":
            args = ["run", "approve", run_id, "--revision", "1", "--approver", "demo"]
        code, _, err = run_json_cli(args, data_dir)
        assert code == 0, f"{cmd}: {err}"

    db_path = data_dir / "default" / "seo-writer.db"
    assert db_path.exists()
    blob = db_path.read_bytes()
    for banned in [b"sk-", b"api_key", b"OPENROUTER", b"DEEPSEEK", b"/Users/", b"sparki"]:
        assert banned not in blob, f"secret/host path leaked into sqlite: {banned!r}"
