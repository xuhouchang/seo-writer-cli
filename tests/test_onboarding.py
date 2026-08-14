"""Onboarding: site memory, web crawl + SEO audit (seo-audit-skill subset),
product evidence confirmation, and provider credential configuration with live
verification."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from seo_writer import onboard, seo_rules
from seo_writer.errors import UsageError
from tests.conftest import run_json_cli

# A best-practice homepage: every core rule passes, no errors.
GOOD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Acme Editing Suite — Automated Editing for Video Teams</title>
<meta name="description" content="Acme Editing Suite automates repetitive editing tasks for video teams,
freeing editors to focus on creative review, client communication and delivery.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="canonical" href="https://acme.example/">
<link rel="icon" href="/favicon.ico" type="image/x-icon">
<link rel="alternate" hreflang="zh-CN" href="https://acme.example/zh/">
<link rel="preconnect" href="https://cdn.acme.example">
<meta property="og:title" content="Acme Editing Suite">
<meta property="og:description" content="Automated editing for video teams.">
<meta property="og:image" content="https://acme.example/og-cover.png">
<meta property="og:url" content="https://acme.example/">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">{"@type": "SoftwareApplication", "name": "Acme Editing Suite", "@id": "https://acme.example/#app"}</script>
</head>
<body>
<a href="#main" class="skip-link">Skip to content</a>
<header><nav><a href="/features">Features</a></nav></header>
<main>
<h1>Acme Editing Suite</h1>
<p>Acme Editing Suite automates repetitive editing tasks for video teams,
freeing editors to focus on creative review, client communication and final delivery.</p>
<p>Batch processing, timeline cleanup and review workflows run unattended,
and every change stays versioned and reversible for the whole team.</p>
<p>Customers report hours saved per project by handing the repetitive passes to
the suite while keeping creative control in the editor's seat.</p>
<p>The suite ingests raw footage, applies the house edit style automatically,
flags b-roll candidates, and generates a first cut that human editors refine instead of rebuild.</p>
<p>Review tools surface every version side by side, collect timestamped comments
from stakeholders, and route approval to the right person with a single link.</p>
<p>Deliverables render in the formats your partners request, from broadcast
masters to social clips, with metadata that survives the pipeline untouched.</p>
<p>Setup takes an afternoon: connect your storage, point the suite at your
folder conventions, and the first unattended cut arrives the same day.</p>
<p>Security is built in from day one, with per-project permissions, full
activity logs, and optional on-premise deployment for regulated industries.</p>
<p>The suite learns your team's naming conventions and keeps every asset
discoverable, so producers find the right shot without hunting through shared drives.</p>
<p>Rendering queues balance load across your workstations, pause gracefully
when you need the machines, and resume without losing a single frame of progress.</p>
<p>Comments stay attached to the timeline, not scattered across chat threads,
so decisions have a home and the next editor inherits the rationale.</p>
<p>Pricing is per editor per month with a free trial, annual plans for growing
teams, and a self-hosted option for studios with strict data requirements.</p>
<form action="/subscribe"><label>Email <input type="email" name="email"></label></form>
<table><thead><tr><th>Plan</th><th>Price</th></tr></thead></table>
<img src="/logo.png" alt="Acme logo" width="120" height="60" loading="lazy" srcset="/logo-2x.png 2x">
<a href="https://twitter.com/acme">Follow us on X</a>
<a href="https://twitter.com/intent/tweet?text=Acme">Share on X</a>
<a href="/contact">Contact</a>
<a href="/privacy">Privacy policy</a>
</main>
<footer><p>© Acme Inc.</p></footer>
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

GOOD_ROBOTS = "User-agent: *\nAllow: /\nUser-agent: ChatGPT-User\nDisallow:\nSitemap: /sitemap.xml\n"
GOOD_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://acme.example/</loc></url></urlset>"
)

GOOD_SITE_MD = """## Acme Editing Suite — product evidence brief (draft)

### What the product does
- Automates repetitive editing tasks for video teams.

### Target audience
- Video teams managing repeatable editing workflows.

### Feature
- Batch processing, timeline cleanup, and review workflows.

### Advantage
- Combines those three workflow steps in one stated product flow.

### Benefit
- Helps the target team spend less effort on repetitive editing work.

### Limitations and non-capabilities
- The source does not state that the product creates original footage.

### Competitor context
- Customer confirmation is required before naming or comparing competitors.

### Claims to avoid
- Do not claim guaranteed time savings without supporting evidence.

### Open questions for the customer
- Which competitor products should be used for factual comparisons?
"""

LEGACY_FEATURE_ONLY_SITE_MD = """## Acme Editing Suite — feature summary (draft)
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
    robots = GOOD_ROBOTS.encode("utf-8")
    sitemap = GOOD_SITEMAP.encode("utf-8")

    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        if self.path == "/ping":
            body, ctype = self.ping, "application/json"
        elif self.path == "/robots.txt":
            body, ctype = self.robots, "text/plain"
        elif self.path == "/sitemap.xml":
            body, ctype = self.sitemap, "application/xml"
        else:
            # serve the page under this server's own origin so canonical/og:url
            # point at the scheme+host that actually served it
            origin = f"http://{self.headers.get('Host', '127.0.0.1')}".encode()
            body = self.page.replace(b"https://acme.example/", origin)
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=()")
        self.send_header("Content-Encoding", "br")
        self.send_header("Alt-Svc", 'h3=":443"')
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
# audit rules (pure)
# ---------------------------------------------------------------------------


def test_audit_good_page_has_no_errors() -> None:
    audit = onboard.audit_html(
        GOOD_HTML,
        status_code=200,
        elapsed_ms=50,
        byte_size=3000,
        url="https://acme.example/",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Strict-Transport-Security": "max-age=31536000",
            "Content-Security-Policy": "default-src 'self'",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=()",
            "Content-Encoding": "br",
            "Alt-Svc": 'h3=":443"',
        },
        robots=GOOD_ROBOTS,
        sitemap=GOOD_SITEMAP,
    )
    assert audit["summary"]["error"] == 0
    assert audit["summary"]["ok"] >= 60
    assert audit["score"] >= 80
    for ident, level in (
        ("technical-reachability", "ok"),
        ("core-title-present", "ok"),
        ("core-title-length", "ok"),
        ("core-description-present", "ok"),
        ("core-canonical-present", "ok"),
        ("core-viewport-present", "ok"),
        ("core-favicon-present", "ok"),
        ("core-h1-present", "ok"),
        ("core-h1-single", "ok"),
        ("htmlval-missing-doctype", "ok"),
        ("htmlval-missing-charset", "ok"),
        ("i18n-lang-attribute", "ok"),
        ("social-og-title", "ok"),
        ("social-og-image", "ok"),
        ("schema-present", "ok"),
        ("schema-valid", "ok"),
        ("images-alt-present", "ok"),
        ("a11y-form-labels", "ok"),
        ("a11y-landmark-regions", "ok"),
        ("a11y-skip-link", "ok"),
        ("technical-robots-txt-exists", "ok"),
        ("technical-robots-txt-valid", "ok"),
        ("technical-sitemap-exists", "ok"),
        ("technical-sitemap-valid", "ok"),
        ("crawl-sitemap-in-robotstxt", "ok"),
    ):
        assert _levels(audit, ident) == level, ident


def test_audit_flags_structural_gaps() -> None:
    audit = onboard.audit_html(BAD_HTML, status_code=200, elapsed_ms=4000, byte_size=2_000_000)
    assert _levels(audit, "core-title-length") == "warning"  # overlong title
    assert _levels(audit, "core-description-present") == "error"  # missing meta description
    assert _levels(audit, "core-canonical-present") == "error"  # no canonical
    assert _levels(audit, "core-viewport-present") == "error"  # no viewport
    assert _levels(audit, "core-h1-single") == "warning"  # two h1
    assert _levels(audit, "images-alt-present") == "warning"  # img without alt
    assert _levels(audit, "schema-present") == "info"  # no JSON-LD
    assert _levels(audit, "perf-page-weight") == "warning"  # heavy page
    assert _levels(audit, "perf-response-time") == "info"  # slow
    assert audit["summary"]["error"] >= 5


def test_audit_http_error_fails_reachability() -> None:
    audit = onboard.audit_html("", status_code=503, elapsed_ms=10, byte_size=0)
    assert _levels(audit, "technical-reachability") == "error"
    assert _levels(audit, "core-title-present") == "error"
    assert _levels(audit, "core-h1-present") == "error"


# ---------------------------------------------------------------------------
# rule categories (representative per-category cases)
# ---------------------------------------------------------------------------


def test_rules_content_word_count_and_title_h1() -> None:
    thin = "<html><body><h1>Hi</h1><p>few words here</p></body></html>"
    audit = onboard.audit_html(thin, status_code=200)
    assert _levels(audit, "content-word-count") == "warning"  # < 100 words
    assert _levels(audit, "content-title-same-as-h1") == "ok"  # no title → skipped, no deduction
    rich = "<html><head><title>Same</title></head><body><h1>Same</h1></body></html>"
    audit2 = onboard.audit_html(rich, status_code=200)
    assert _levels(audit2, "content-title-same-as-h1") == "info"


def test_rules_url_structure_uses_page_url() -> None:
    audit = onboard.audit_html(GOOD_HTML, status_code=200, url="https://acme.example/About_Us?utm_source=x")
    assert _levels(audit, "url-uppercase") == "warning"
    assert _levels(audit, "url-underscores") == "warning"
    assert _levels(audit, "url-parameters") == "info"
    assert _levels(audit, "url-tracking-params") == "info"


def test_rules_security_headers() -> None:
    audit = onboard.audit_html(
        GOOD_HTML,
        status_code=200,
        url="https://acme.example/",
        headers={
            "Strict-Transport-Security": "max-age=31536000",
            "Content-Security-Policy": "default-src 'self'",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=()",
            "Content-Encoding": "br",
            "Alt-Svc": 'h3=":443"',
            "Cache-Control": "public, max-age=3600",
        },
    )
    for ident, level in (
        ("security-https", "ok"),
        ("security-hsts", "ok"),
        ("security-csp", "ok"),
        ("security-x-frame", "ok"),
        ("security-x-content-type", "ok"),
        ("security-referrer-policy", "ok"),
        ("security-permissions-policy", "ok"),
        ("perf-text-compression", "ok"),
        ("perf-http2", "ok"),
        ("perf-cache-policy", "ok"),
    ):
        assert _levels(audit, ident) == level, ident
    bare = onboard.audit_html(GOOD_HTML, status_code=200, url="http://acme.example/")
    assert _levels(bare, "security-https") == "error"
    assert _levels(bare, "security-hsts") == "warning"


def test_rules_schema_json_ld() -> None:
    bad_json = '<html><head><script type="application/ld+json">{not json</script></head><body></body></html>'
    audit = onboard.audit_html(bad_json, status_code=200)
    assert _levels(audit, "schema-valid") == "error"
    no_fields = '<script type="application/ld+json">{"@type": "Product"}</script>'
    audit2 = onboard.audit_html(f"<html><head>{no_fields}</head><body></body></html>", status_code=200)
    assert _levels(audit2, "schema-required-fields") == "info"  # missing name/@id


def test_rules_heading_order_and_forms() -> None:
    hmm = '<html><body><h3>Skip level</h3><form action="/go"><input name="q"></form></body></html>'
    audit = onboard.audit_html(hmm, status_code=200)
    assert _levels(audit, "a11y-heading-order") == "warning"  # starts with h3
    assert _levels(audit, "a11y-form-labels") == "warning"  # input without label


def test_rules_meta_refresh_and_mixed_content() -> None:
    html = (
        '<html><head><meta http-equiv="refresh" content="0;url=/new">'
        '</head><body><img src="http://cdn.example/pic.png"></body></html>'
    )
    audit = onboard.audit_html(html, status_code=200, url="https://acme.example/")
    assert _levels(audit, "redirect-meta-refresh") == "warning"
    assert _levels(audit, "security-mixed-content") == "error"


def test_rules_robots_parsing_units() -> None:
    directives, invalid = seo_rules.parse_robots("User-agent: *\nSitemap: /sitemap.xml\nBogus line\n")
    assert [d["field"] for d in directives] == ["user-agent", "sitemap"]
    assert invalid == ["Bogus line"]
    assert seo_rules.sitemap_looks_valid(GOOD_SITEMAP) is True
    assert seo_rules.sitemap_looks_valid("<html>not a sitemap</html>") is False


def test_rules_robots_missing_reports_findings() -> None:
    audit = onboard.audit_html(GOOD_HTML, status_code=200, url="https://acme.example/")
    assert _levels(audit, "technical-robots-txt-exists") == "warning"
    assert _levels(audit, "technical-sitemap-exists") == "info"


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


def test_fetch_site_end_to_end(ws, fake_site) -> None:
    onboard.save_site(ws, "acme", fake_site)
    result = onboard.fetch_site(ws, "acme")
    assert result["http_status"] == 200
    assert result["status"] == "fetched"
    assert result["score"] >= 80
    assert result["summary"]["error"] == 0
    assert result["rubric"]["name"] == "seo-audit-skill (MIT) static subset"
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
# product evidence confirmation
# ---------------------------------------------------------------------------


def test_confirm_requires_agent_draft(ws) -> None:
    onboard.save_site(ws, "acme", "https://acme.example.com")
    with pytest.raises(UsageError, match="no product evidence brief yet"):
        onboard.confirm_features(ws, "acme", "bob")


def test_confirm_rejects_feature_only_draft(ws) -> None:
    onboard.save_site(ws, "acme", "https://acme.example.com")
    onboard.feature_path(ws, "acme").write_text(LEGACY_FEATURE_ONLY_SITE_MD, encoding="utf-8")

    with pytest.raises(UsageError, match="missing required product input sections") as exc_info:
        onboard.confirm_features(ws, "acme", "bob")

    message = str(exc_info.value)
    assert "Target audience" in message
    assert "Feature" in message
    assert "Advantage" in message
    assert "Benefit" in message
    assert "Limitations and non-capabilities" in message
    assert "Competitor context" in message


def test_site_status_reports_product_input_completeness(ws) -> None:
    onboard.save_site(ws, "acme", "https://acme.example.com")
    onboard.feature_path(ws, "acme").write_text(LEGACY_FEATURE_ONLY_SITE_MD, encoding="utf-8")
    incomplete = onboard.site_status(ws, "acme")
    assert incomplete["product_input_complete"] is False
    assert "Benefit" in incomplete["missing_product_input_sections"]

    onboard.feature_path(ws, "acme").write_text(GOOD_SITE_MD, encoding="utf-8")
    complete = onboard.site_status(ws, "acme")
    assert complete["product_input_complete"] is True
    assert complete["missing_product_input_sections"] == []


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
    assert fetched["summary"]["error"] == 0

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
    assert "no product evidence brief" in err

    brand_dir = data_dir / "default" / "brands" / "acme"
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "site.md").write_text(GOOD_SITE_MD, encoding="utf-8")
    _, confirmed, _ = _cli(data_dir, "onboard", "confirm", "acme", "--approver", "bob")
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_by"] == "bob"

    _, providers, _ = _cli(data_dir, "providers", "status")
    assert providers["dataforseo"]["configured"] is False
    assert providers["reddit"]["configured"] is False
