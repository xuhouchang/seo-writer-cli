"""Onboarding: site memory, web crawl + baseline SEO audit, feature
confirmation, and provider credential configuration with live verification."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from seo_writer import onboard
from seo_writer.errors import UsageError
from tests.conftest import run_json_cli

GOOD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Acme Editing Suite — Automated Editing for Video Teams</title>
  <meta name="description" content="Acme Editing Suite automates repetitive editing tasks for video teams.">
  <link rel="canonical" href="https://acme.example/">
  <script type="application/ld+json">{"@type": "SoftwareApplication"}</script>
</head>
<body>
  <h1>Acme Editing Suite</h1>
  <img src="/logo.png" alt="Acme logo">
  <p>Acme Editing Suite automates repetitive editing tasks for video teams.</p>
</body>
</html>
"""

BAD_HTML = (
    "<html><head><title>"
    "Way too long title for a website page that will definitely exceed the 60 character limit"
    "</title></head><body><h1>First heading</h1><h1>Second heading</h1>"
    '<img src="/x.png"><p>no meta description, no canonical, no structured data</p>'
    "</body></html>"
)

OK_DFS = {"ok": True, "provider": "dataforseo", "message": "ok"}
FAIL_REDDIT = {"ok": False, "provider": "reddit", "message": "HTTP Error 401: Unauthorized"}


GOOD_SITE_MD = """## Acme Editing Suite — feature summary (draft)
- Automates repetitive editing tasks for video teams
- Batch processing, timeline cleanup, review workflows
"""


def _levels(audit: dict, ident: str) -> str:
    return next(c["level"] for c in audit["checks"] if c["id"] == ident)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StaticHandler(BaseHTTPRequestHandler):
    page = GOOD_HTML.encode("utf-8")
    ping = b'{"status_code": 20000, "status_message": "Ok."}'

    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        body = self.ping if self.path == "/ping" else self.page
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence test noise
        pass


@pytest.fixture
def fake_site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), _StaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


# ---------------------------------------------------------------------------
# site memory
# ---------------------------------------------------------------------------


def test_save_site_records_url_and_file(ws) -> None:
    result = onboard.save_site(ws, "acme", "https://acme.example.com/")
    assert result["status"] == "draft"
    assert result["changed"] is True
    profile = onboard.load_site(ws, "acme")
    assert profile["url"] == "https://acme.example.com/"
    assert onboard.site_profile_path(ws, "acme").exists()


def test_save_site_rejects_invalid_url(ws) -> None:
    for bad in ("ftp://acme.example", "not-a-url", "https://"):
        with pytest.raises(UsageError):
            onboard.save_site(ws, "acme", bad)


def test_save_site_change_resets_status(ws) -> None:
    onboard.save_site(ws, "acme", "https://one.example.com")
    path = onboard.site_profile_path(ws, "acme")
    path.write_text(path.read_text().replace("status: draft", "status: confirmed"))
    result = onboard.save_site(ws, "acme", "https://two.example.com")
    assert result["changed"] is True
    assert onboard.load_site(ws, "acme")["status"] == "draft"
    assert onboard.load_site(ws, "acme")["confirmed_by"] is None


def test_site_status_requires_recorded_site(ws) -> None:
    with pytest.raises(UsageError):
        onboard.site_status(ws, "acme")


# ---------------------------------------------------------------------------
# baseline SEO audit (pure)
# ---------------------------------------------------------------------------


def test_audit_html_good_page() -> None:
    audit = onboard.audit_html(GOOD_HTML, status_code=200, elapsed_ms=50, byte_size=500)
    assert _levels(audit, "SEO-001") == "ok"
    assert _levels(audit, "SEO-002") == "ok"
    assert _levels(audit, "SEO-003") == "ok"
    assert _levels(audit, "SEO-004") == "ok"
    assert _levels(audit, "SEO-005") == "ok"
    assert _levels(audit, "SEO-006") == "ok"
    assert _levels(audit, "SEO-008") == "ok"
    assert audit["summary"]["error"] == 0


def test_audit_html_flags_structural_gaps() -> None:
    audit = onboard.audit_html(BAD_HTML, status_code=200, elapsed_ms=4000, byte_size=2_000_000)
    assert _levels(audit, "SEO-002") == "warning"  # overlong title
    assert _levels(audit, "SEO-003") == "warning"  # missing meta description
    assert _levels(audit, "SEO-004") == "warning"  # two h1
    assert _levels(audit, "SEO-005") == "warning"  # img without alt
    assert _levels(audit, "SEO-006") == "info"  # no canonical
    assert _levels(audit, "SEO-008") == "info"  # no JSON-LD
    assert _levels(audit, "SEO-009") == "warning"  # heavy page
    assert _levels(audit, "SEO-010") == "info"  # slow
    assert audit["summary"]["error"] == 0
    assert audit["summary"]["warning"] == 5


def test_audit_html_http_error_fails() -> None:
    audit = onboard.audit_html("", status_code=503, elapsed_ms=10, byte_size=0)
    assert _levels(audit, "SEO-001") == "error"
    assert audit["summary"]["error"] == 3  # reachability + title + h1


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


def test_fetch_site_end_to_end(ws, fake_site) -> None:
    onboard.save_site(ws, "acme", fake_site)
    result = onboard.fetch_site(ws, "acme")
    assert result["http_status"] == 200
    assert result["status"] == "fetched"
    assert result["score"] >= 80
    assert onboard.load_site(ws, "acme")["status"] == "fetched"
    assert onboard.crawl_dir(ws, "acme").joinpath("index.html").exists()
    assert onboard.crawl_dir(ws, "acme").joinpath("seo-audit.yaml").exists()
    content = onboard.crawl_dir(ws, "acme").joinpath("content.txt").read_text()
    assert "Acme Editing Suite" in content


def test_fetch_site_requires_recorded_url(ws) -> None:
    with pytest.raises(UsageError):
        onboard.fetch_site(ws, "acme")


def test_fetch_site_unreachable(ws) -> None:
    onboard.save_site(ws, "acme", "http://127.0.0.1:1/")  # nothing listens on port 1
    with pytest.raises(UsageError, match="cannot reach"):
        onboard.fetch_site(ws, "acme", timeout=3)


# ---------------------------------------------------------------------------
# feature confirmation
# ---------------------------------------------------------------------------


def test_confirm_requires_agent_draft(ws) -> None:
    onboard.save_site(ws, "acme", "https://acme.example.com")
    with pytest.raises(UsageError, match="no feature summary yet"):
        onboard.confirm_features(ws, "acme", "bob")


def test_confirm_marks_confirmed(ws) -> None:
    onboard.save_site(ws, "acme", "https://acme.example.com")
    onboard.feature_path(ws, "acme").write_text(GOOD_SITE_MD, encoding="utf-8")
    result = onboard.confirm_features(ws, "acme", "bob")
    assert result["status"] == "confirmed"
    assert result["confirmed_by"] == "bob"
    profile = onboard.load_site(ws, "acme")
    assert profile["status"] == "confirmed"
    assert profile["confirmed_at"] is not None


# ---------------------------------------------------------------------------
# provider credentials
# ---------------------------------------------------------------------------


def test_provider_status_unconfigured(tmp_path) -> None:
    status = onboard.provider_status(tmp_path)
    for row in status.values():
        assert row["configured"] is False
        assert row["verified"] is False


def test_configure_provider_requires_all_fields(tmp_path) -> None:
    with pytest.raises(UsageError, match="requires"):
        onboard.configure_provider(tmp_path, "dataforseo", {"login": "only-login"})
    with pytest.raises(UsageError):
        onboard.configure_provider(tmp_path, "nope", {"x": "y"})


def test_configure_provider_verifies_and_stores_0600(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(onboard, "_verify_dataforseo", lambda *_: OK_DFS)
    result = onboard.configure_provider(
        tmp_path, "dataforseo", {"login": "l@example.com", "password": "s3cret"}
    )
    assert result["ok"] is True
    secrets = onboard.load_secrets(tmp_path)
    assert secrets["dataforseo"]["login"] == "l@example.com"
    mode = onboard.secrets_path(tmp_path).stat().st_mode & 0o777
    assert mode == 0o600
    state = onboard.provider_status(tmp_path)["dataforseo"]
    assert state["configured"] is True
    assert state["verified"] is True


def test_verify_failure_records_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(onboard, "_verify_reddit", lambda *_: FAIL_REDDIT)
    onboard.configure_provider(tmp_path, "reddit", {"client_id": "cid", "client_secret": "csec"})
    state = onboard.provider_status(tmp_path)["reddit"]
    assert state["configured"] is True
    assert state["verified"] is False
    assert "401" in state["last_error"]


def test_provider_status_never_prints_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(onboard, "_verify_dataforseo", lambda *_: OK_DFS)
    onboard.configure_provider(
        tmp_path, "dataforseo", {"login": "l@example.com", "password": "super-secret-pw"}
    )
    blob = json.dumps(onboard.provider_status(tmp_path))
    assert "super-secret-pw" not in blob
    assert "l@example.com" not in blob


def test_verify_dataforseo_live_endpoint(tmp_path, fake_site, monkeypatch) -> None:
    monkeypatch.setattr(onboard, "DATAFORSEO_PING_URL", f"{fake_site}/ping")
    result = onboard._verify_dataforseo("l@example.com", "pw")
    assert result["ok"] is True
    assert result["message"] == "credentials valid (status_code 20000)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(data_dir: Path, *args: str) -> tuple[int, dict | None, str]:
    return run_json_cli(list(args), data_dir)


def test_cli_onboard_site_fetch_status(tmp_path, fake_site) -> None:
    data_dir = tmp_path / "data"
    assert _cli(data_dir, "init")[0] == 0
    assert _cli(data_dir, "brand", "create", "acme")[0] == 0

    _, site, _ = _cli(data_dir, "onboard", "site", "acme", "--url", fake_site)
    assert site["status"] == "draft"

    _, fetched, _ = _cli(data_dir, "onboard", "fetch", "acme")
    assert fetched["http_status"] == 200
    assert fetched["status"] == "fetched"

    _, status, _ = _cli(data_dir, "onboard", "status", "acme")
    assert status["status"] == "fetched"
    assert status["crawl"]["exists"] is True


def test_cli_onboard_site_rejects_bad_url(tmp_path) -> None:
    data_dir = tmp_path / "data"
    _cli(data_dir, "init")
    _cli(data_dir, "brand", "create", "acme")
    code, out, err = _cli(data_dir, "onboard", "site", "acme", "--url", "not-a-url")
    assert code == 2
    assert out is None
    assert '"UsageError"' in err


def test_cli_confirm_flow_and_providers_status(tmp_path) -> None:
    data_dir = tmp_path / "data"
    _cli(data_dir, "init")
    _cli(data_dir, "brand", "create", "acme")
    _cli(data_dir, "onboard", "site", "acme", "--url", "https://acme.example.com")

    code, _, err = _cli(data_dir, "onboard", "confirm", "acme", "--approver", "bob")
    assert code == 2
    assert "no feature summary" in err

    brand_dir = data_dir / "default" / "brands" / "acme"
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "site.md").write_text(GOOD_SITE_MD, encoding="utf-8")
    _, confirmed, _ = _cli(data_dir, "onboard", "confirm", "acme", "--approver", "bob")
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_by"] == "bob"

    _, providers, _ = _cli(data_dir, "providers", "status")
    assert providers["dataforseo"]["configured"] is False
    assert providers["reddit"]["configured"] is False
