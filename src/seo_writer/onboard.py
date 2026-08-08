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
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from . import seo_rules
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


def _http_get(url: str, timeout: float = 15.0) -> tuple[bytes, int, float, dict[str, str]]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
            return (
                body[:MAX_BODY_BYTES],
                resp.status,
                (time.monotonic() - start) * 1000,
                dict(resp.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        # 4xx/5xx still carry a body worth auditing (reachability check fails the audit).
        return (
            exc.read(MAX_BODY_BYTES),
            exc.code,
            (time.monotonic() - start) * 1000,
            dict(exc.headers.items()),
        )
    except urllib.error.URLError as exc:
        raise UsageError(f"cannot reach {url}: {exc.reason}") from exc


def _fetch_aux_text(url: str, timeout: float) -> str | None:
    """Fetch an auxiliary same-origin file (robots.txt / sitemap.xml).

    Absence and failures are tolerated — the audit rules report them as
    findings instead of aborting the crawl.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return _decode_html(resp.read(1_000_000))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def _decode_html(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("latin-1")


_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_LANDMARK_TAGS = {"header", "nav", "main", "footer", "section", "article", "aside"}
_SKIP_TARGETS = {"#main", "#content", "#main-content", "#content-main", "#primary", "#skip", "#skip-content"}
_SOCIAL_HOSTS = (
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "tiktok.com",
    "weibo.com",
    "weixin",
    "zhihu.com",
    "bilibili.com",
)


def _link_kind(href: str) -> str:
    if href.startswith("https://"):
        return "https"
    if href.startswith("http://"):
        return "http"
    if href.startswith("mailto:"):
        return "mailto"
    if href.startswith("tel:"):
        return "tel"
    if href.startswith("javascript:"):
        return "javascript"
    if href.startswith("#"):
        return "fragment"
    return "relative"


class _SiteParser(HTMLParser):
    """Reads a page into a `seo_rules.Doc` for the audit rule set.

    Collects everything the static rules need: title/meta/canonical/favicon,
    OpenGraph/Twitter, headings, images, links, forms, tables, landmarks,
    JSON-LD blocks and visible text. Scripts and styles are skipped (except
    JSON-LD, which is captured raw for schema rules).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = seo_rules.Doc(html="")
        self._skip_depth = 0
        self._in_title = False
        self._heading_tag: str | None = None
        self._in_head = False
        self._in_body = False
        self._in_picture = 0
        self._buf: list[str] = []
        self._text_parts: list[str] = []
        self._links: list[tuple[seo_rules.Link, list[str]]] = []
        self._form: seo_rules.Form | None = None
        self._table_has_th: list[bool] = []
        self._jsonld_buf: list[str] | None = None
        self._open: list[str] = []

    def _attrs(self, attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {k: (v or "") for k, v in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = self._attrs(attrs)
        self.doc.element_count += 1
        if tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._jsonld_buf = []
            self._skip_depth += 1
            return
        if tag == "style":
            self._skip_depth += 1
            return
        if tag not in _VOID_TAGS:
            self._open.append(tag)
        if tag == "head":
            self.doc.head_count += 1
            self._in_head = True
        elif tag == "body":
            self._in_body = True
        elif tag == "html":
            self.doc.lang = a.get("lang")
        elif tag == "title":
            self._in_title = True
            self._buf = []
        elif tag in _HEADING_TAGS:
            self._heading_tag = tag
            self._buf = []
        elif tag == "meta":
            self._handle_meta(a)
        elif tag == "link":
            self._handle_link(a)
        elif tag == "img":
            self._handle_img(a)
        elif tag == "a":
            self._links.append((self._make_link(a), []))
        elif tag == "form":
            self._form = seo_rules.Form(
                method=a.get("method", "get").upper(),
                action=a.get("action") or None,
                has_label=False,
                input_count=0,
            )
        elif tag in ("input", "select", "textarea") and self._form:
            self._form.input_count += 1
        elif tag == "label" and self._form:
            self._form.has_label = True
        elif tag == "table":
            self._table_has_th.append(False)
        elif tag == "th" and self._table_has_th:
            self._table_has_th[-1] = True
        elif tag == "picture":
            self._in_picture += 1
        elif tag in _LANDMARK_TAGS:
            self.doc.landmarks += 1
        elif tag == "noscript" and self._in_head:
            self.doc.noscript_in_head = True
        if a.get("aria-label") and self._in_body:
            self.doc.aria_labels += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_decl(self, decl: str) -> None:
        if decl.strip().lower().startswith("doctype"):
            self.doc.has_doctype = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._jsonld_buf is not None:
                self.doc.jsonld.append("".join(self._jsonld_buf))
                self._jsonld_buf = None
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "style":
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            text = "".join(self._buf).strip()
            self.doc.titles.append(text)
            if self.doc.title is None:
                self.doc.title = text or None
        elif tag in _HEADING_TAGS:
            text = "".join(self._buf).strip()
            self.doc.headings.append((tag, text))
            if tag == "h1":
                self.doc.h1s.append(text)
            self._heading_tag = None
        elif tag == "head":
            self._in_head = False
        elif tag == "a" and self._links:
            link, buf = self._links.pop()
            link.text = "".join(buf).strip()
            self.doc.links.append(link)
            if link.kind == "fragment" and (
                link.text.lower().startswith("skip") or link.href in _SKIP_TARGETS
            ):
                self.doc.skip_link = True
        elif tag == "form" and self._form:
            self.doc.forms.append(self._form)
            self._form = None
        elif tag == "table" and self._table_has_th:
            self.doc.tables += 1
            if self._table_has_th.pop():
                self.doc.tables_with_th += 1
        elif tag == "picture":
            self._in_picture = max(0, self._in_picture - 1)
        if tag in self._open:
            while self._open and self._open[-1] != tag:
                self._open.pop()
            self._open.pop()
        elif self._in_body and tag not in _VOID_TAGS:
            self.doc.broken_tags.append(tag)

    def handle_data(self, data: str) -> None:
        if self._jsonld_buf is not None:
            self._jsonld_buf.append(data)
            return
        if self._skip_depth:
            return
        if self._in_title or self._heading_tag:
            self._buf.append(data)
            return
        if self._links:
            self._links[-1][1].append(data)
        if data.strip():
            self._text_parts.append(" ".join(data.split()))

    # -- element handlers --------------------------------------------------

    def _handle_meta(self, a: dict[str, str]) -> None:
        name = (a.get("name") or a.get("property") or "").lower()
        content = a.get("content", "")
        if a.get("charset"):
            self.doc.charset = a["charset"]
        elif name == "description":
            self.doc.meta_descriptions.append(content)
            if self.doc.meta_description is None:
                self.doc.meta_description = content or None
        elif name == "robots":
            self.doc.meta_robots = content
        elif name == "viewport":
            self.doc.viewports.append(content)
        elif name.startswith("og:"):
            self.doc.og[name[3:]] = content
        elif name.startswith("twitter:"):
            self.doc.twitter[name[8:]] = content
        elif a.get("http-equiv", "").lower() == "content-type":
            m = re.search(r"charset=([\w-]+)", content, re.I)
            if m:
                self.doc.charset = m.group(1)
        elif a.get("http-equiv", "").lower() == "refresh":
            self.doc.meta_refresh = True
        if self._in_body:
            self.doc.meta_in_body = True

    def _handle_link(self, a: dict[str, str]) -> None:
        rel = (a.get("rel") or "").lower().split()
        href = a.get("href") or ""
        if "canonical" in rel:
            self.doc.canonicals.append(href)
        if "icon" in rel:
            self.doc.favicons.append(href)
        if "alternate" in rel and a.get("hreflang"):
            self.doc.hreflangs.append(a["hreflang"])
        if "preconnect" in rel or "preload" in rel:
            self.doc.preconnects += 1

    def _handle_img(self, a: dict[str, str]) -> None:
        src = a.get("src") or ""
        alt = a.get("alt")
        if alt is None and a.get("aria-label"):
            alt = a["aria-label"]
        self.doc.imgs.append(
            seo_rules.Img(
                src=src,
                alt=alt,
                has_dimensions=bool(a.get("width") and a.get("height")),
                loading=a.get("loading"),
                has_srcset=bool(a.get("srcset")),
                in_picture=self._in_picture > 0,
                is_svg=".svg" in src.lower() or src.startswith("data:image/svg"),
                filename=src.rsplit("/", 1)[-1] if src else "",
            )
        )

    def _make_link(self, a: dict[str, str]) -> seo_rules.Link:
        href = a.get("href") or ""
        return seo_rules.Link(
            href=href,
            text="",
            rel=a.get("rel") or "",
            onclick="onclick" in a,
            kind=_link_kind(href),
            blank=a.get("target") == "_blank",
        )


def _page_text(parser: _SiteParser) -> str:
    joined = "\n".join(parser._text_parts)
    return joined[:MAX_TEXT_CHARS]


def audit_html(
    html: str,
    *,
    status_code: int = 200,
    elapsed_ms: float = 0,
    byte_size: int = 0,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    robots: str | None = None,
    sitemap: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """Run the full audit rule set over one page (pure).

    Rules are the static subset of seo-audit-skill (MIT); see `seo_rules`.
    """
    parser = _SiteParser()
    parser.feed(html)
    parser.close()
    doc = parser.doc
    doc.html = html
    doc.text = " ".join(parser._text_parts)
    ctx = seo_rules.Ctx(
        url=url,
        headers=headers or {},
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        byte_size=byte_size,
        robots=robots,
        sitemap=sitemap,
        keyword=keyword,
    )
    return seo_rules.audit_doc(doc, ctx)


def _sitemap_for(url: str, robots: str | None, timeout: float) -> str | None:
    """Sitemap location: the one robots.txt declares, else /sitemap.xml.

    The declared location may be relative (common in hand-written robots.txt),
    so it is resolved against the site URL.
    """
    if robots:
        m = re.search(r"(?im)^\s*sitemap:\s*(\S+)", robots)
        if m:
            return _fetch_aux_text(urllib.parse.urljoin(url, m.group(1)), timeout)
    return _fetch_aux_text(urllib.parse.urljoin(url, "/sitemap.xml"), timeout)


def fetch_site(ws: Workspace, slug: str, *, timeout: float = 15.0) -> dict[str, Any]:
    profile = load_site(ws, slug)
    if profile is None or not profile.get("url"):
        raise UsageError(
            f"brand '{slug}' has no site recorded; run `seo-writer onboard site {slug} --url <url>`"
        )
    url = profile["url"]
    body, status, elapsed, headers = _http_get(url, timeout)
    html = _decode_html(body)

    # robots.txt + sitemap.xml: same-origin, standard library, absence tolerated.
    robots = _fetch_aux_text(urllib.parse.urljoin(url, "/robots.txt"), timeout)
    sitemap = _sitemap_for(url, robots, timeout)

    parser = _SiteParser()
    parser.feed(html)
    parser.close()
    audit = audit_html(
        html,
        status_code=status,
        elapsed_ms=elapsed,
        byte_size=len(body),
        url=url,
        headers=headers,
        robots=robots,
        sitemap=sitemap,
    )
    audit["rubric"] = {
        "name": "seo-audit-skill (MIT) static subset",
        "source": "https://github.com/seo-skills/seo-audit-skill",
    }

    out = crawl_dir(ws, slug)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_bytes(body)
    (out / "content.txt").write_text(_page_text(parser), encoding="utf-8")
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
        "rubric": audit["rubric"],
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
        configured = bool(secrets.get(name)) and all((secrets.get(name) or {}).get(f) for f in fields)
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
