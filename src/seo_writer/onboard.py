"""New-brand onboarding: site memory, web crawl, SEO audit, provider config.

Onboarding is the first-run journey for a brand:

1.  `onboard site`    — record the customer's website URL (local memory).
2.  `onboard fetch`   — crawl it over plain HTTP (no API key needed), run a
                       baseline SEO audit, keep the page text for the agent.
3.  `onboard confirm` — a human confirms the agent-authored feature summary.
4.  `providers`       — configure DataForSEO / Reddit credentials (stored
                       chmod-600, never in git) and verify the connections.

Audit rules here are a *baseline* set; the authoritative rubric may be
extended later. The crawl/verify helpers use only the standard library so the
product stays zero-dependency.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from .config import Workspace
from .errors import UsageError

USER_AGENT = "seo-writer-onboard/0.1 (+https://github.com/xuhouchang/seo-writer)"
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 200_000

DATAFORSEO_PING_URL = "https://api.dataforseo.com/v3/ping"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

PROVIDERS: dict[str, dict[str, str]] = {
    "dataforseo": {"login": "login", "password": "password"},
    "reddit": {"client_id": "client_id", "client_secret": "client_secret"},
}


def _write_yaml(path: Path, payload: Any) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

# ---------------------------------------------------------------------------
# site memory
# ---------------------------------------------------------------------------


def brand_dir(ws: Workspace, slug: str) -> Path:
    return ws.root / "brands" / slug


def site_profile_path(ws: Workspace, slug: str) -> Path:
    return brand_dir(ws, slug) / "site.yaml"


def crawl_dir(ws: Workspace, slug: str) -> Path:
    return brand_dir(ws, slug) / "site-crawl"


def feature_path(ws: Workspace, slug: str) -> Path:
    return brand_dir(ws, slug) / "site.md"


def load_site(ws: Workspace, slug: str) -> dict[str, Any] | None:
    path = site_profile_path(ws, slug)
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _validate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError(f"invalid site url '{url}': expected http(s)://host[/path]")
    return parsed.geturl()


def save_site(ws: Workspace, slug: str, url: str) -> dict[str, Any]:
    url = _validate_url(url)
    dir_path = brand_dir(ws, slug)
    dir_path.mkdir(parents=True, exist_ok=True)
    profile = load_site(ws, slug) or {}
    changed = profile.get("url") != url
    if changed:
        profile = {
            "url": url,
            "status": "draft",
            "fetched_at": None,
            "confirmed_by": None,
            "confirmed_at": None,
        }
    path = site_profile_path(ws, slug)
    _write_yaml(path, profile)
    return {"site": slug, "path": str(path), "url": url, "status": profile["status"], "changed": changed}


def site_status(ws: Workspace, slug: str) -> dict[str, Any]:
    """Onboarding progress for a brand: profile, crawl artifacts, feature draft."""
    profile = load_site(ws, slug)
    if profile is None:
        raise UsageError(
            f"brand '{slug}' has no site recorded; run `seo-writer onboard site {slug} --url <url>`"
        )
    crawl = crawl_dir(ws, slug)
    fp = feature_path(ws, slug)
    return {
        "site": slug,
        "url": profile.get("url"),
        "status": profile.get("status", "draft"),
        "fetched_at": profile.get("fetched_at"),
        "confirmed_by": profile.get("confirmed_by"),
        "confirmed_at": profile.get("confirmed_at"),
        "crawl": {
            "index_html": str(crawl / "index.html"),
            "content_txt": str(crawl / "content.txt"),
            "seo_audit_yaml": str(crawl / "seo-audit.yaml"),
            "exists": (crawl / "seo-audit.yaml").exists(),
        },
        "feature_file": str(fp),
        "feature_drafted": fp.exists() and fp.read_text(encoding="utf-8").strip() != "",
    }


# ---------------------------------------------------------------------------
# crawl + baseline SEO audit
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = 15.0) -> tuple[bytes, int, float]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
            return body[:MAX_BODY_BYTES], resp.status, (time.monotonic() - start) * 1000
    except urllib.error.HTTPError as exc:
        # 4xx/5xx still carry a body worth auditing (status_code check fails the audit).
        return exc.read(MAX_BODY_BYTES), exc.code, (time.monotonic() - start) * 1000
    except urllib.error.URLError as exc:
        raise UsageError(f"cannot reach {url}: {exc.reason}") from exc


def _decode_html(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("latin-1")


@dataclass
class _PageData:
    title: str | None = None
    meta: dict[str, str] = field(default_factory=dict)
    canonical: str | None = None
    h1: list[str] = field(default_factory=list)
    img_total: int = 0
    img_no_alt: int = 0
    json_ld_count: int = 0
    text: list[str] = field(default_factory=list)


class _SiteParser(HTMLParser):
    """Minimal page reader: title, meta, canonical, h1, img alt, JSON-LD, text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = _PageData()
        self._skip_depth = 0
        self._in_title = False
        self._in_h1 = False
        self._buf: list[str] = []

    def _attrs(self, attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {k: (v or "") for k, v in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = self._attrs(attrs)
        if tag in ("script", "style"):
            if tag == "script" and a.get("type", "").lower() == "application/ld+json":
                self.page.json_ld_count += 1
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
            self._buf = []
        elif tag == "h1":
            self._in_h1 = True
            self._buf = []
        elif tag == "meta":
            name = a.get("name") or a.get("property")
            if name:
                self.page.meta[name.lower()] = a.get("content", "")
        elif tag == "link" and a.get("rel", "").lower() == "canonical":
            self.page.canonical = a.get("href")
        elif tag == "img":
            self.page.img_total += 1
            if not (a.get("alt") or a.get("aria-label")):
                self.page.img_no_alt += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
            if self.page.title is None:
                self.page.title = "".join(self._buf).strip()
        elif tag == "h1":
            self._in_h1 = False
            self.page.h1.append("".join(self._buf).strip())

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title or self._in_h1:
            self._buf.append(data)
            return
        if data.strip():
            self.page.text.append(" ".join(data.split()))


def _page_text(page: _PageData) -> str:
    lines = [line for line in page.text if line]
    joined = "\n".join(lines)
    return joined[:MAX_TEXT_CHARS]


def audit_html(html: str, *, status_code: int, elapsed_ms: float, byte_size: int) -> dict[str, Any]:
    """Baseline SEO audit (pure). Extended rubric may be added later."""
    parser = _SiteParser()
    parser.feed(html)
    page = parser.page
    checks: list[dict[str, str]] = []

    def add(ident: str, level: str, message: str) -> None:
        checks.append({"id": ident, "level": level, "message": message})

    if status_code >= 400:
        add("SEO-001", "error", f"site returned HTTP {status_code}")
    else:
        add("SEO-001", "ok", f"site reachable (HTTP {status_code})")

    title = (page.title or "").strip()
    if not title:
        add("SEO-002", "error", "missing <title>")
    elif len(title) > 60:
        add("SEO-002", "warning", f"<title> is {len(title)} chars (>60)")
    else:
        add("SEO-002", "ok", f"<title> ok ({len(title)} chars)")

    desc = page.meta.get("description", "").strip()
    if not desc:
        add("SEO-003", "warning", "missing meta description")
    elif len(desc) > 155:
        add("SEO-003", "warning", f"meta description is {len(desc)} chars (>155)")
    else:
        add("SEO-003", "ok", f"meta description ok ({len(desc)} chars)")

    h1 = len(page.h1)
    if h1 == 0:
        add("SEO-004", "error", "no <h1> on the page")
    elif h1 > 1:
        add("SEO-004", "warning", f"{h1} <h1> tags (expect exactly one)")
    else:
        add("SEO-004", "ok", "exactly one <h1>")

    if page.img_total and page.img_no_alt:
        add("SEO-005", "warning", f"{page.img_no_alt}/{page.img_total} images missing alt text")
    else:
        add("SEO-005", "ok", "all images have alt text (or none present)")

    if page.canonical:
        add("SEO-006", "ok", f"canonical present: {page.canonical}")
    else:
        add("SEO-006", "info", "no canonical link")

    if page.meta.get("robots"):
        add("SEO-007", "ok", "robots meta present")
    else:
        add("SEO-007", "info", "no robots meta (fine unless crawling must be blocked)")

    if page.json_ld_count:
        add("SEO-008", "ok", f"{page.json_ld_count} JSON-LD structured-data blocks")
    else:
        add("SEO-008", "info", "no JSON-LD structured data")

    if byte_size > 1_000_000:
        add("SEO-009", "warning", f"page is {byte_size / 1024:.0f} KiB (heavy)")
    else:
        add("SEO-009", "ok", f"page is {byte_size / 1024:.0f} KiB")

    if elapsed_ms > 3000:
        add("SEO-010", "info", f"slow response ({elapsed_ms:.0f} ms)")
    else:
        add("SEO-010", "ok", f"responded in {elapsed_ms:.0f} ms")

    counts = {
        level: sum(1 for c in checks if c["level"] == level) for level in ("ok", "info", "warning", "error")
    }
    score = max(0, 100 - counts["error"] * 30 - counts["warning"] * 10 - counts["info"] * 2)
    return {"score": score, "summary": counts, "checks": checks}


def fetch_site(ws: Workspace, slug: str, *, timeout: float = 15.0) -> dict[str, Any]:
    profile = load_site(ws, slug)
    if profile is None or not profile.get("url"):
        raise UsageError(
            f"brand '{slug}' has no site recorded; run `seo-writer onboard site {slug} --url <url>`"
        )
    url = profile["url"]
    body, status, elapsed = _http_get(url, timeout)
    html = _decode_html(body)
    page = _SiteParser()
    page.feed(html)
    audit = audit_html(html, status_code=status, elapsed_ms=elapsed, byte_size=len(body))

    out = crawl_dir(ws, slug)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_bytes(body)
    (out / "content.txt").write_text(_page_text(page.page), encoding="utf-8")
    _write_yaml(out / "seo-audit.yaml", audit)

    profile["status"] = "fetched"
    profile["fetched_at"] = _now_iso()
    _write_yaml(site_profile_path(ws, slug), profile)

    return {
        "site": slug,
        "url": url,
        "status": profile["status"],
        "http_status": status,
        "elapsed_ms": round(elapsed),
        "score": audit["score"],
        "summary": audit["summary"],
        "checks": audit["checks"],
        "paths": {
            "index_html": str(out / "index.html"),
            "content_txt": str(out / "content.txt"),
            "seo_audit_yaml": str(out / "seo-audit.yaml"),
        },
    }


# ---------------------------------------------------------------------------
# feature confirmation
# ---------------------------------------------------------------------------


def confirm_features(ws: Workspace, slug: str, approver: str) -> dict[str, Any]:
    profile = load_site(ws, slug)
    if profile is None or not profile.get("url"):
        raise UsageError(
            f"brand '{slug}' has no site recorded; run `seo-writer onboard site {slug} --url <url>`"
        )
    fp = feature_path(ws, slug)
    if not fp.exists() or not fp.read_text(encoding="utf-8").strip():
        raise UsageError(
            f"no feature summary yet; an agent writes it to {fp}, the customer reviews it, then confirm"
        )
    profile["status"] = "confirmed"
    profile["confirmed_by"] = approver
    profile["confirmed_at"] = _now_iso()
    _write_yaml(site_profile_path(ws, slug), profile)
    return {
        "site": slug,
        "status": "confirmed",
        "confirmed_by": approver,
        "confirmed_at": profile["confirmed_at"],
        "feature_file": str(fp),
    }


# ---------------------------------------------------------------------------
# provider credentials (DataForSEO / Reddit)
# ---------------------------------------------------------------------------


def secrets_path(data_dir: Path) -> Path:
    return Path(data_dir) / ".secrets.yaml"


def provider_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "providers.yaml"


def load_secrets(data_dir: Path) -> dict[str, dict[str, str]]:
    path = secrets_path(data_dir)
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _save_secrets(data_dir: Path, secrets: dict[str, dict[str, str]]) -> Path:
    path = secrets_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(secrets, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)
    return path


def _read_provider_state(data_dir: Path) -> dict[str, Any]:
    path = provider_state_path(data_dir)
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _write_provider_state(data_dir: Path, state: dict[str, Any]) -> None:
    provider_state_path(data_dir).write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")


def provider_status(data_dir: Path) -> dict[str, Any]:
    secrets = load_secrets(data_dir)
    state = _read_provider_state(data_dir)
    out: dict[str, Any] = {}
    for name, fields in PROVIDERS.items():
        configured = bool(secrets.get(name)) and all(
            (secrets.get(name) or {}).get(f) for f in fields
        )
        row = {
            "configured": configured,
            "verified": bool(state.get(name, {}).get("verified")),
            "verified_at": state.get(name, {}).get("verified_at"),
            "last_error": state.get(name, {}).get("last_error"),
            "auth": {"file": str(secrets_path(data_dir)), "fields": list(fields.values())},
        }
        out[name] = row
    return out


def _open_url(url: str, headers: dict[str, str], data: bytes | None, timeout: float) -> Any:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **headers})
    if url.startswith("https://www.reddit.com") and os.environ.get("REDDIT_PROXY_URL"):
        proxy = urllib.request.ProxyHandler({"https": os.environ["REDDIT_PROXY_URL"]})
        return urllib.request.build_opener(proxy).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def verify_provider(data_dir: Path, name: str, values: dict[str, str]) -> dict[str, Any]:
    if name == "dataforseo":
        result = _verify_dataforseo(values["login"], values["password"])
    elif name == "reddit":
        result = _verify_reddit(values["client_id"], values["client_secret"])
    else:
        raise UsageError(f"unknown provider '{name}'; expected one of: {', '.join(PROVIDERS)}")
    state = _read_provider_state(data_dir)
    state[name] = {
        "configured": True,
        "verified": result["ok"],
        "verified_at": _now_iso(),
        "last_error": None if result["ok"] else result["message"],
    }
    _write_provider_state(data_dir, state)
    return result


def _verify_dataforseo(login: str, password: str, timeout: float = 20.0) -> dict[str, Any]:
    auth = base64.b64encode(f"{login}:{password}".encode()).decode()
    try:
        with _open_url(DATAFORSEO_PING_URL, {"Authorization": f"Basic {auth}"}, None, timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        code = payload.get("status_code")
        if code == 20000:
            return {"ok": True, "provider": "dataforseo", "message": "credentials valid (status_code 20000)"}
        return {
            "ok": False,
            "provider": "dataforseo",
            "message": f"status_code {code}: {payload.get('status_message', '')}",
        }
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "provider": "dataforseo", "message": str(exc)}


def _verify_reddit(client_id: str, client_secret: str, timeout: float = 20.0) -> dict[str, Any]:
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
    try:
        with _open_url(REDDIT_TOKEN_URL, headers, data, timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("token_type") == "bearer" and payload.get("access_token"):
            return {"ok": True, "provider": "reddit", "message": "OAuth client credentials accepted"}
        return {"ok": False, "provider": "reddit", "message": f"unexpected response: {list(payload)}"}
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "provider": "reddit", "message": str(exc)}


def configure_provider(data_dir: Path, name: str, values: dict[str, str]) -> dict[str, Any]:
    if name not in PROVIDERS:
        raise UsageError(f"unknown provider '{name}'; expected one of: {', '.join(PROVIDERS)}")
    required = PROVIDERS[name]
    missing = [f for f in required if not (values.get(f) or "").strip()]
    if missing:
        raise UsageError(f"provider '{name}' requires: {', '.join(missing)}")
    secrets = load_secrets(data_dir)
    secrets[name] = values
    _save_secrets(data_dir, secrets)
    return verify_provider(data_dir, name, values)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
