"""SEO audit rules — category-scoped, dependency-free static checks.

Rule IDs, thresholds and severities follow the upstream open-source project
https://github.com/seo-skills/seo-audit-skill (MIT License, 251 rules / 20
categories, "SEOmator"). This module re-implements the subset that is
checkable from a single static HTML document plus the response headers and
robots.txt / sitemap.xml text captured during `onboard fetch`. Cross-page
rules (JS rendering, Core Web Vitals, link graphs, hreflang networks,
redirect chains) are Phase 2.

Each rule is a pure function over `Doc` + `Ctx` returning a `Check`; the
registry order is deterministic and the audit never raises (a crashing rule
becomes an explicit `error` check so the failure is visible, not silent).

Levels: error (must fix, -30), warning (should fix, -10), info (improvement,
-2), ok (no deduction). Score = max(0, 100 - errors*30 - warnings*10 -
infos*2), consistent with the pre-existing baseline audit.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "Doc",
    "Ctx",
    "Check",
    "audit_doc",
    "RULES",
    "parse_robots",
    "sitemap_looks_valid",
]

# ---------------------------------------------------------------------------
# data structures
# ---------------------------------------------------------------------------


@dataclass
class Img:
    src: str
    alt: str | None
    has_dimensions: bool
    loading: str | None
    has_srcset: bool
    in_picture: bool
    is_svg: bool
    filename: str


@dataclass
class Link:
    href: str
    text: str
    rel: str
    onclick: bool
    kind: str  # http | https | mailto | tel | javascript | fragment | relative
    blank: bool


@dataclass
class Form:
    method: str
    action: str | None
    has_label: bool
    input_count: int


@dataclass
class Doc:
    """Everything a static-HTML audit needs, produced by the site parser."""

    html: str
    text: str = ""
    title: str | None = None
    titles: list[str] = field(default_factory=list)
    meta_description: str | None = None
    meta_descriptions: list[str] = field(default_factory=list)
    meta_robots: str | None = None
    viewports: list[str] = field(default_factory=list)
    charset: str | None = None
    has_doctype: bool = False
    lang: str | None = None
    canonicals: list[str] = field(default_factory=list)
    favicons: list[str] = field(default_factory=list)
    og: dict[str, str] = field(default_factory=dict)
    twitter: dict[str, str] = field(default_factory=dict)
    h1s: list[str] = field(default_factory=list)
    headings: list[tuple[str, str]] = field(default_factory=list)
    imgs: list[Img] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)
    tables: int = 0
    tables_with_th: int = 0
    jsonld: list[str] = field(default_factory=list)
    aria_labels: int = 0
    landmarks: int = 0
    preconnects: int = 0
    noscript_in_head: bool = False
    meta_in_body: bool = False
    head_count: int = 0
    hreflangs: list[str] = field(default_factory=list)
    element_count: int = 0
    broken_tags: list[str] = field(default_factory=list)
    skip_link: bool = False
    meta_refresh: bool = False


@dataclass
class Ctx:
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    status_code: int = 200
    elapsed_ms: int = 0
    byte_size: int = 0
    robots: str | None = None
    sitemap: str | None = None
    keyword: str | None = None


@dataclass
class Check:
    id: str
    category: str
    level: str  # ok | info | warning | error
    message: str
    data: dict[str, Any] = field(default_factory=dict)


_LEVEL_ORDER = {"ok": 0, "info": 1, "warning": 2, "error": 3}


def _check(fn: Any, level: str, message: str, **data: Any) -> Check:
    """Build a Check, deriving the stable id and category from the rule.

    The id is the explicit value passed to ``@rule(id=...)`` at decoration
    time (``fn._rule_id``) — never derived from ``fn.__name__``, so renaming a
    rule function cannot silently change its persisted baseline id.
    """
    rid = fn._rule_id
    return Check(rid, rid.split("-", 1)[0], level, message, data)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

RULES: list[Any] = []
_RULE_IDS: set[str] = set()


def rule(*, id: str) -> Any:
    """Register a rule function with an explicit, stable id.

    ``@rule(id="core-title-present")`` is the only supported form. The id is
    the persisted baseline identifier — it is pinned at decoration time and is
    independent of the function name, so renaming a rule function never
    silently changes the audit output (or breaks cross-version baselines).

    A bare ``@rule`` (missing ``id=``) or a duplicate id is a hard error at
    import time rather than a silent fallback.
    """

    def register(fn: Any) -> Any:
        if id in _RULE_IDS:
            raise ValueError(f"duplicate rule id {id!r} (second registration: {fn.__name__!r})")
        _RULE_IDS.add(id)
        fn._rule_id = id  # type: ignore[attr-defined]
        RULES.append(fn)
        return fn

    return register


def audit_doc(doc: Doc, ctx: Ctx) -> dict[str, Any]:
    """Run every registered rule against the parsed document + request context.

    Returns the audit report: checks (deterministic order), per-category
    tallies, summary counts and a 0-100 score.
    """
    checks: list[Check] = []
    for fn in RULES:
        try:
            checks.append(fn(doc, ctx))
        except Exception as exc:  # noqa: BLE001 — a rule must never break the audit
            checks.append(_check(fn, "error", f"internal rule error: {exc!r}"))
    return _report(checks)


def _report(checks: list[Check]) -> dict[str, Any]:
    summary = {"ok": 0, "info": 0, "warning": 0, "error": 0}
    categories: dict[str, dict[str, int]] = {}
    for c in checks:
        summary[c.level] += 1
        tallies = categories.setdefault(c.category, {"ok": 0, "info": 0, "warning": 0, "error": 0})
        tallies[c.level] += 1
    score = max(0, 100 - summary["error"] * 30 - summary["warning"] * 10 - summary["info"] * 2)
    return {
        "score": score,
        "summary": summary,
        "categories": categories,
        "checks": [
            {
                "id": c.id,
                "category": c.category,
                "level": c.level,
                "message": c.message,
                **(c.data or {}),
            }
            for c in checks
        ],
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _first_heading(doc: Doc) -> str | None:
    return doc.headings[0][0] if doc.headings else None


def _url_of(doc: Doc, ctx: Ctx) -> str | None:
    return ctx.url or (doc.canonicals[0] if doc.canonicals else None)


def _page_scheme(url: str | None) -> str | None:
    return urlparse(url).scheme if url else None


def _is_loopback(url: str | None) -> bool:
    """True for localhost / 127.x / ::1 — plain-HTTP audits on dev boxes are not findings."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _text_has(doc: Doc, *needles: str) -> bool:
    text = doc.text.lower()
    return any(n in text for n in needles)


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------


@rule(id="core-title-present")
def core_title_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.titles:
        return _check(core_title_present, "error", "No <title> tag in the document head")
    if not doc.title:
        return _check(core_title_present, "error", "<title> exists but is empty")
    return _check(core_title_present, "ok", "Title tag present")


@rule(id="core-title-length")
def core_title_length(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.title:
        return _check(core_title_length, "ok", "Skipped (no title)")
    n = len(doc.title)
    if n < 30:
        return _check(
            core_title_length, "warning", f"Title too short ({n} chars; recommended 30-60)", length=n
        )
    if n > 60:
        return _check(
            core_title_length, "warning", f"Title too long ({n} chars; recommended 30-60)", length=n
        )
    return _check(core_title_length, "ok", f"Title length optimal ({n} chars)", length=n)


@rule(id="core-description-present")
def core_description_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.meta_descriptions:
        return _check(core_description_present, "error", "No meta description")
    if not doc.meta_description:
        return _check(core_description_present, "error", "Meta description exists but is empty")
    return _check(core_description_present, "ok", "Meta description present")


@rule(id="core-description-length")
def core_description_length(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.meta_description:
        return _check(core_description_length, "ok", "Skipped (no meta description)")
    n = len(doc.meta_description)
    if n < 120:
        return _check(
            core_description_length,
            "warning",
            f"Description too short ({n} chars; recommended 120-160)",
            length=n,
        )
    if n > 160:
        return _check(
            core_description_length,
            "warning",
            f"Description too long ({n} chars; recommended 120-160)",
            length=n,
        )
    return _check(core_description_length, "ok", f"Description length optimal ({n} chars)", length=n)


@rule(id="core-canonical-present")
def core_canonical_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.canonicals:
        return _check(core_canonical_present, "error", "No rel=canonical link")
    return _check(core_canonical_present, "ok", "Canonical link present", canonical=doc.canonicals[0])


@rule(id="core-canonical-valid")
def core_canonical_valid(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.canonicals:
        return _check(core_canonical_valid, "ok", "Skipped (no canonical)")
    bad = [c for c in doc.canonicals if urlparse(c).scheme not in ("http", "https")]
    if bad:
        return _check(
            core_canonical_valid, "warning", f"Canonical URL not valid HTTP(S): {bad[0]}", value=bad[0]
        )
    return _check(core_canonical_valid, "ok", "Canonical URL format valid")


@rule(id="core-canonical-conflicting")
def core_canonical_conflicting(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    distinct = sorted(set(doc.canonicals))
    if len(distinct) > 1:
        return _check(
            core_canonical_conflicting, "error", f"Conflicting canonical URLs: {distinct}", values=distinct
        )
    return _check(core_canonical_conflicting, "ok", "Single canonical URL")


@rule(id="core-canonical-http-mismatch")
def core_canonical_http_mismatch(doc: Doc, ctx: Ctx) -> Check:
    if not doc.canonicals:
        return _check(core_canonical_http_mismatch, "ok", "Skipped (no canonical)")
    page_scheme = _page_scheme(ctx.url)
    if not page_scheme:
        return _check(core_canonical_http_mismatch, "ok", "Skipped (unknown page scheme)")
    mismatched = [c for c in doc.canonicals if _page_scheme(c) and _page_scheme(c) != page_scheme]
    if mismatched:
        return _check(
            core_canonical_http_mismatch,
            "warning",
            f"Canonical scheme ({_page_scheme(mismatched[0])}) differs from page scheme ({page_scheme})",
            value=mismatched[0],
        )
    return _check(core_canonical_http_mismatch, "ok", "Canonical scheme matches page")


@rule(id="core-canonical-to-homepage")
def core_canonical_to_homepage(doc: Doc, ctx: Ctx) -> Check:
    if not doc.canonicals or not ctx.url:
        return _check(core_canonical_to_homepage, "ok", "Skipped (no canonical or page URL)")
    page_path = urlparse(ctx.url).path or "/"
    if page_path != "/" and urlparse(doc.canonicals[0]).path in ("", "/"):
        return _check(
            core_canonical_to_homepage, "warning", "Canonical points to the homepage from a non-homepage URL"
        )
    return _check(core_canonical_to_homepage, "ok", "Canonical does not collapse to homepage")


@rule(id="core-canonical-to-noindex")
def core_canonical_to_noindex(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.meta_robots and "noindex" in doc.meta_robots.lower() and doc.canonicals:
        return _check(core_canonical_to_noindex, "warning", "Page is both noindex and declares a canonical")
    return _check(core_canonical_to_noindex, "ok", "No canonical/noindex conflict")


@rule(id="core-canonical-header")
def core_canonical_header(doc: Doc, ctx: Ctx) -> Check:
    header_link = ctx.headers.get("Link", "")
    m = re.search(r"<([^>]+)>\s*;\s*rel=\"?canonical\"?", header_link, re.I)
    if not m:
        return _check(core_canonical_header, "ok", "No HTTP Link canonical header")
    header_canonical = m.group(1)
    if doc.canonicals and header_canonical != doc.canonicals[0]:
        return _check(
            core_canonical_header,
            "warning",
            f"HTTP Link canonical ({header_canonical}) differs from HTML canonical ({doc.canonicals[0]})",
            header=header_canonical,
            html=doc.canonicals[0],
        )
    return _check(core_canonical_header, "ok", "HTTP and HTML canonical agree")


@rule(id="core-viewport-present")
def core_viewport_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.viewports:
        return _check(core_viewport_present, "error", "No viewport meta tag (mobile responsiveness)")
    return _check(core_viewport_present, "ok", "Viewport meta present", value=doc.viewports[0])


@rule(id="core-favicon-present")
def core_favicon_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.favicons:
        return _check(core_favicon_present, "warning", "No favicon link tag")
    return _check(core_favicon_present, "ok", "Favicon declared", values=doc.favicons)


@rule(id="core-h1-present")
def core_h1_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.h1s:
        return _check(core_h1_present, "error", "No <h1> heading on the page")
    return _check(core_h1_present, "ok", "H1 heading present")


@rule(id="core-h1-single")
def core_h1_single(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if len(doc.h1s) > 1:
        return _check(
            core_h1_single,
            "warning",
            f"Multiple <h1> tags ({len(doc.h1s)}); one per page is best",
            count=len(doc.h1s),
        )
    if not doc.h1s:
        return _check(core_h1_single, "ok", "Skipped (no h1)")
    return _check(core_h1_single, "ok", "Exactly one <h1>")


@rule(id="core-nosnippet")
def core_nosnippet(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.meta_robots and "nosnippet" in doc.meta_robots.lower():
        return _check(core_nosnippet, "warning", "robots meta blocks snippets (nosnippet)")
    return _check(core_nosnippet, "ok", "Snippets not blocked")


@rule(id="core-robots-meta")
def core_robots_meta(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.meta_robots:
        return _check(core_robots_meta, "info", "No robots meta tag (defaults to index,follow)")
    return _check(core_robots_meta, "ok", "Robots meta present", value=doc.meta_robots)


# ---------------------------------------------------------------------------
# html validation
# ---------------------------------------------------------------------------


@rule(id="htmlval-missing-doctype")
def htmlval_missing_doctype(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.has_doctype:
        return _check(htmlval_missing_doctype, "error", "Missing <!DOCTYPE html> declaration")
    return _check(htmlval_missing_doctype, "ok", "DOCTYPE declared")


@rule(id="htmlval-missing-charset")
def htmlval_missing_charset(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.charset:
        return _check(htmlval_missing_charset, "error", "No character encoding declared")
    return _check(htmlval_missing_charset, "ok", "Charset declared", value=doc.charset)


@rule(id="htmlval-invalid-head")
def htmlval_invalid_head(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.meta_in_body:
        return _check(htmlval_invalid_head, "warning", "Meta tags found inside <body>; they belong in <head>")
    return _check(htmlval_invalid_head, "ok", "Head contains only head elements")


@rule(id="htmlval-noscript-in-head")
def htmlval_noscript_in_head(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.noscript_in_head:
        return _check(htmlval_noscript_in_head, "warning", "<noscript> inside <head> is invalid HTML")
    return _check(htmlval_noscript_in_head, "ok", "No noscript in head")


@rule(id="htmlval-multiple-heads")
def htmlval_multiple_heads(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.head_count > 1:
        return _check(htmlval_multiple_heads, "error", f"Multiple <head> elements ({doc.head_count})")
    return _check(htmlval_multiple_heads, "ok", "Single head element")


@rule(id="htmlval-lorem-ipsum")
def htmlval_lorem_ipsum(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if "lorem ipsum" in doc.text.lower():
        return _check(htmlval_lorem_ipsum, "warning", "Placeholder text (lorem ipsum) found on the page")
    return _check(htmlval_lorem_ipsum, "ok", "No placeholder text")


@rule(id="htmlval-multiple-titles")
def htmlval_multiple_titles(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if len(doc.titles) > 1:
        return _check(
            htmlval_multiple_titles,
            "error",
            f"Multiple <title> tags ({len(doc.titles)})",
            count=len(doc.titles),
        )
    return _check(htmlval_multiple_titles, "ok", "Single title tag")


@rule(id="htmlval-multiple-descriptions")
def htmlval_multiple_descriptions(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if len(doc.meta_descriptions) > 1:
        return _check(
            htmlval_multiple_descriptions,
            "error",
            f"Multiple meta descriptions ({len(doc.meta_descriptions)})",
            count=len(doc.meta_descriptions),
        )
    return _check(htmlval_multiple_descriptions, "ok", "Single meta description")


# ---------------------------------------------------------------------------
# social
# ---------------------------------------------------------------------------


@rule(id="social-og-title")
def social_og_title(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.og.get("title"):
        return _check(social_og_title, "warning", "No og:title meta (social sharing title)")
    return _check(social_og_title, "ok", "og:title present", value=doc.og["title"])


@rule(id="social-og-description")
def social_og_description(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.og.get("description"):
        return _check(social_og_description, "info", "No og:description meta")
    return _check(social_og_description, "ok", "og:description present")


@rule(id="social-og-image")
def social_og_image(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.og.get("image"):
        return _check(social_og_image, "warning", "No og:image meta (share thumbnail)")
    return _check(social_og_image, "ok", "og:image present", value=doc.og["image"])


@rule(id="social-twitter-card")
def social_twitter_card(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.twitter.get("card"):
        return _check(social_twitter_card, "info", "No twitter:card meta")
    return _check(social_twitter_card, "ok", "Twitter card present", value=doc.twitter["card"])


@rule(id="social-og-url")
def social_og_url(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.og.get("url"):
        return _check(social_og_url, "info", "No og:url meta")
    return _check(social_og_url, "ok", "og:url present", value=doc.og["url"])


@rule(id="social-og-url-canonical")
def social_og_url_canonical(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    og_url = doc.og.get("url")
    if og_url and doc.canonicals and og_url.rstrip("/") != doc.canonicals[0].rstrip("/"):
        return _check(
            social_og_url_canonical,
            "warning",
            f"og:url ({og_url}) differs from canonical ({doc.canonicals[0]})",
            og_url=og_url,
            canonical=doc.canonicals[0],
        )
    return _check(social_og_url_canonical, "ok", "og:url consistent with canonical")


@rule(id="social-share-buttons")
def social_share_buttons(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if _text_has(doc, "share", "share this"):
        return _check(social_share_buttons, "ok", "Share controls detected")
    return _check(social_share_buttons, "info", "No share controls on the page")


@rule(id="social-social-profiles")
def social_social_profiles(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    social_hosts = (
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
    links = [ln.href for ln in doc.links]
    found = [h for h in social_hosts if any(h in u for u in links)]
    if found:
        return _check(social_social_profiles, "ok", "Social profile links found", profiles=found)
    return _check(social_social_profiles, "info", "No social profile links on the page")


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------


@rule(id="content-word-count")
def content_word_count(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    n = len(doc.text.split())
    if n < 100:
        return _check(content_word_count, "warning", f"Thin content: only {n} words of visible text", words=n)
    if n < 300:
        return _check(content_word_count, "info", f"Light content: {n} words", words=n)
    return _check(content_word_count, "ok", f"Substantial content ({n} words)", words=n)


@rule(id="content-meta-in-body")
def content_meta_in_body(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.meta_in_body:
        return _check(content_meta_in_body, "error", "Meta tags appear inside <body> (invalid placement)")
    return _check(content_meta_in_body, "ok", "No meta tags in body")


@rule(id="content-heading-length")
def content_heading_length(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    long = [h for tag, h in doc.headings if len(h) > 70]
    if long:
        return _check(
            content_heading_length, "info", f"Heading too long ({len(long)}): {long[0]!r}", heading=long[0]
        )
    return _check(content_heading_length, "ok", "Heading lengths within limits")


@rule(id="content-heading-unique")
def content_heading_unique(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    texts = [h for _, h in doc.headings if h]
    dupes = sorted({t for t in texts if texts.count(t) > 1})
    if dupes:
        return _check(
            content_heading_unique, "info", f"Duplicate heading text: {dupes[0]!r}", heading=dupes[0]
        )
    return _check(content_heading_unique, "ok", "Headings are unique")


@rule(id="content-text-html-ratio")
def content_text_html_ratio(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    html_len = max(len(doc.html), 1)
    ratio = len(doc.text) / html_len * 100
    if ratio < 10:
        return _check(
            content_text_html_ratio, "info", f"Low text-to-HTML ratio ({ratio:.1f}%)", ratio=round(ratio, 1)
        )
    return _check(content_text_html_ratio, "ok", f"Text-to-HTML ratio {ratio:.1f}%", ratio=round(ratio, 1))


@rule(id="content-title-same-as-h1")
def content_title_same_as_h1(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.title and doc.h1s and doc.title.strip().lower() == doc.h1s[0].strip().lower():
        return _check(content_title_same_as_h1, "info", "Title duplicates the H1 text; differentiate them")
    return _check(content_title_same_as_h1, "ok", "Title and H1 differ")


@rule(id="content-title-pixel-width")
def content_title_pixel_width(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.title:
        return _check(content_title_pixel_width, "ok", "Skipped (no title)")
    if len(doc.title) > 70:  # ~8.3px/char estimates the ~580px SERP cutoff
        return _check(
            content_title_pixel_width, "warning", "Title may truncate in SERPs (~580px pixel width)"
        )
    return _check(content_title_pixel_width, "ok", "Title fits SERP pixel budget")


@rule(id="content-description-pixel-width")
def content_description_pixel_width(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.meta_description:
        return _check(content_description_pixel_width, "ok", "Skipped (no description)")
    if len(doc.meta_description) > 155:  # ~6px/char estimates the ~930px SERP cutoff
        return _check(
            content_description_pixel_width,
            "warning",
            "Description may truncate in SERPs (~930px pixel width)",
        )
    return _check(content_description_pixel_width, "ok", "Description fits SERP pixel budget")


@rule(id="content-broken-html")
def content_broken_html(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.broken_tags:
        return _check(
            content_broken_html,
            "info",
            f"Unclosed tags detected: {', '.join(doc.broken_tags[:3])}",
            tags=doc.broken_tags[:5],
        )
    return _check(content_broken_html, "ok", "Tags appear balanced")


@rule(id="content-keyword-stuffing")
def content_keyword_stuffing(doc: Doc, ctx: Ctx) -> Check:
    if not ctx.keyword:
        return _check(content_keyword_stuffing, "ok", "Skipped (no focus keyword given)")
    words = doc.text.lower().split()
    if not words:
        return _check(content_keyword_stuffing, "ok", "Skipped (no text)")
    hits = sum(1 for w in words if ctx.keyword.lower() in w)
    ratio = hits / len(words)
    if ratio > 0.03:
        return _check(
            content_keyword_stuffing,
            "warning",
            f'Keyword "{ctx.keyword}" appears {hits}× ({ratio:.1%} of words)',
            hits=hits,
            ratio=round(ratio, 4),
        )
    return _check(content_keyword_stuffing, "ok", "Keyword density within bounds", hits=hits)


# ---------------------------------------------------------------------------
# accessibility
# ---------------------------------------------------------------------------


@rule(id="a11y-aria-labels")
def a11y_aria_labels(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    controls = sum(1 for f in doc.forms for _ in range(f.input_count))
    if controls and doc.aria_labels < 1:
        return _check(a11y_aria_labels, "info", "Interactive controls present without any aria-label")
    return _check(a11y_aria_labels, "ok", "ARIA labels present where needed")


@rule(id="a11y-form-labels")
def a11y_form_labels(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    unlabeled = [f for f in doc.forms if f.input_count and not f.has_label]
    if unlabeled:
        return _check(
            a11y_form_labels,
            "warning",
            f"Form fields without <label> ({len(unlabeled)} form(s))",
            forms=len(unlabeled),
        )
    return _check(a11y_form_labels, "ok", "Form fields labelled")


@rule(id="a11y-heading-order")
def a11y_heading_order(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.headings:
        return _check(a11y_heading_order, "ok", "Skipped (no headings)")
    first = doc.headings[0][0]
    if first != "h1":
        return _check(
            a11y_heading_order, "warning", f"Document starts with {first} instead of <h1>", first=first
        )
    ranks = [int(tag[1]) for tag, _ in doc.headings]
    skips = [
        (doc.headings[i][0], doc.headings[i - 1][0])
        for i in range(1, len(ranks))
        if ranks[i] > ranks[i - 1] + 1
    ]
    if skips:
        return _check(
            a11y_heading_order,
            "warning",
            f"Heading levels skipped: {skips[0][0]} follows {skips[0][1]}",
            example=skips[0],
        )
    return _check(a11y_heading_order, "ok", "Heading order is logical")


@rule(id="a11y-landmark-regions")
def a11y_landmark_regions(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.landmarks < 2:
        return _check(
            a11y_landmark_regions,
            "warning",
            f"Few landmark regions ({doc.landmarks}); add header/nav/main/footer",
            landmarks=doc.landmarks,
        )
    return _check(a11y_landmark_regions, "ok", "Landmark regions present", landmarks=doc.landmarks)


@rule(id="a11y-link-text")
def a11y_link_text(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    vague = [
        ln.text
        for ln in doc.links
        if ln.text.strip().lower() in ("click here", "read more", "more", "here", "link", "")
        and ln.kind in ("http", "https")
    ]
    if vague:
        return _check(
            a11y_link_text,
            "info",
            f"Vague link text: {vague[0]!r} (screen readers need descriptive text)",
            example=vague[0],
        )
    return _check(a11y_link_text, "ok", "Link text is descriptive")


@rule(id="a11y-skip-link")
def a11y_skip_link(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.skip_link:
        return _check(a11y_skip_link, "ok", "Skip-to-content link present")
    return _check(a11y_skip_link, "info", "No skip-to-content link")


@rule(id="a11y-table-headers")
def a11y_table_headers(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.tables and doc.tables_with_th < doc.tables:
        return _check(
            a11y_table_headers,
            "warning",
            f"{doc.tables - doc.tables_with_th} table(s) without <th> headers",
            tables=doc.tables,
            with_th=doc.tables_with_th,
        )
    return _check(a11y_table_headers, "ok", "Tables declare headers")


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


@rule(id="images-alt-present")
def images_alt_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    missing = [i for i in doc.imgs if i.alt is None and i.src]
    if missing:
        return _check(
            images_alt_present,
            "warning",
            f'{len(missing)} image(s) missing alt text (decorative images should use alt="")',
            count=len(missing),
            examples=[i.src for i in missing[:3]],
        )
    return _check(images_alt_present, "ok", "All images have alt text")


@rule(id="images-alt-length")
def images_alt_length(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    long = [i for i in doc.imgs if i.alt and len(i.alt) > 125]
    if long:
        return _check(
            images_alt_length, "info", f"{len(long)} image(s) with alt longer than 125 chars", count=len(long)
        )
    return _check(images_alt_length, "ok", "Alt text lengths within limits")


@rule(id="images-dimensions")
def images_dimensions(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    no_size = [i for i in doc.imgs if not i.has_dimensions and i.src]
    if no_size:
        return _check(
            images_dimensions,
            "info",
            f"{len(no_size)} image(s) without width/height attributes (layout shift)",
            count=len(no_size),
        )
    return _check(images_dimensions, "ok", "Images declare dimensions")


@rule(id="images-lazy-loading")
def images_lazy_loading(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.imgs:
        return _check(images_lazy_loading, "ok", "Skipped (no images)")
    lazy = sum(1 for i in doc.imgs if i.loading == "lazy")
    if lazy == 0:
        return _check(
            images_lazy_loading, "info", "No lazy-loading images (consider loading= for below-fold images)"
        )
    return _check(images_lazy_loading, "ok", "Lazy loading used", count=lazy)


@rule(id="images-modern-format")
def images_modern_format(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    legacy = [
        i
        for i in doc.imgs
        if i.src and re.search(r"\.(png|jpg|jpeg|gif)(\?|$)", i.src, re.I) and not i.is_svg
    ]
    if legacy:
        return _check(
            images_modern_format,
            "info",
            f"{len(legacy)} image(s) in legacy formats (PNG/JPG); consider WebP/AVIF",
            count=len(legacy),
        )
    return _check(images_modern_format, "ok", "Images use modern formats")


@rule(id="images-responsive")
def images_responsive(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.imgs:
        return _check(images_responsive, "ok", "Skipped (no images)")
    no_srcset = [i for i in doc.imgs if not i.has_srcset and not i.is_svg]
    if no_srcset:
        return _check(
            images_responsive,
            "info",
            f"{len(no_srcset)} image(s) without srcset (responsive sizes)",
            count=len(no_srcset),
        )
    return _check(images_responsive, "ok", "Responsive images (srcset) in use")


@rule(id="images-picture-element")
def images_picture_element(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.imgs and not any(i.in_picture for i in doc.imgs):
        return _check(images_picture_element, "info", "No <picture> element for art-directed images")
    return _check(images_picture_element, "ok", "Picture element present or no images")


@rule(id="images-filename-quality")
def images_filename_quality(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    generic = [
        i
        for i in doc.imgs
        if i.src and re.search(r"/(img|image|photo|pic|untitled|screenshot)[-_0-9]*\.", i.src, re.I)
    ]
    if generic:
        return _check(
            images_filename_quality,
            "info",
            f"Generic image filenames ({len(generic)}); descriptive names help SEO",
            count=len(generic),
        )
    return _check(images_filename_quality, "ok", "Image filenames are descriptive")


# ---------------------------------------------------------------------------
# url structure
# ---------------------------------------------------------------------------


def _url_path(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).path or ""


@rule(id="url-uppercase")
def url_uppercase(doc: Doc, ctx: Ctx) -> Check:
    path = _url_path(_url_of(doc, ctx))
    if path and any(c.isupper() for c in path):
        return _check(url_uppercase, "warning", "URL path contains uppercase characters")
    return _check(url_uppercase, "ok", "URL path is lowercase")


@rule(id="url-underscores")
def url_underscores(doc: Doc, ctx: Ctx) -> Check:
    path = _url_path(_url_of(doc, ctx))
    if path and "_" in path:
        return _check(url_underscores, "warning", "URL path contains underscores; hyphens are preferred")
    return _check(url_underscores, "ok", "URL uses hyphens")


@rule(id="url-spaces")
def url_spaces(doc: Doc, ctx: Ctx) -> Check:
    url = _url_of(doc, ctx)
    if url and (" " in url or "%20" in url):
        return _check(url_spaces, "error", "URL contains spaces (must be percent-encoded)")
    return _check(url_spaces, "ok", "URL is space-free")


@rule(id="url-length")
def url_length(doc: Doc, ctx: Ctx) -> Check:
    url = _url_of(doc, ctx)
    if url and len(url) > 100:
        return _check(url_length, "info", f"URL is long ({len(url)} chars); keep under ~100", length=len(url))
    return _check(url_length, "ok", "URL length within bounds")


@rule(id="url-parameters")
def url_parameters(doc: Doc, ctx: Ctx) -> Check:
    url = _url_of(doc, ctx)
    if url and "?" in url:
        return _check(
            url_parameters, "info", "URL carries query parameters (crawl-budget and duplicate concerns)"
        )
    return _check(url_parameters, "ok", "URL is parameter-free")


@rule(id="url-tracking-params")
def url_tracking_params(doc: Doc, ctx: Ctx) -> Check:
    url = _url_of(doc, ctx)
    if url and re.search(r"[?&](utm_|fbclid|gclid|mc_cid|mc_eid|yclid)", url, re.I):
        return _check(url_tracking_params, "info", "URL carries tracking parameters (utm_* etc.)")
    return _check(url_tracking_params, "ok", "No tracking parameters")


# ---------------------------------------------------------------------------
# mobile
# ---------------------------------------------------------------------------


@rule(id="mobile-viewport-width")
def mobile_viewport_width(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.viewports:
        return _check(mobile_viewport_width, "ok", "Skipped (no viewport)")
    if "width=device-width" not in doc.viewports[0].lower():
        return _check(mobile_viewport_width, "warning", "Viewport does not use width=device-width")
    return _check(mobile_viewport_width, "ok", "Viewport configured for mobile width")


@rule(id="mobile-multiple-viewports")
def mobile_multiple_viewports(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if len(doc.viewports) > 1:
        return _check(
            mobile_multiple_viewports, "error", f"Multiple viewport meta tags ({len(doc.viewports)})"
        )
    return _check(mobile_multiple_viewports, "ok", "Single viewport tag")


# ---------------------------------------------------------------------------
# internationalization
# ---------------------------------------------------------------------------


@rule(id="i18n-lang-attribute")
def i18n_lang_attribute(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.lang:
        return _check(
            i18n_lang_attribute, "warning", "No lang attribute on <html> (screen readers and search engines)"
        )
    return _check(i18n_lang_attribute, "ok", "lang attribute present", value=doc.lang)


@rule(id="i18n-hreflang-present")
def i18n_hreflang_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.hreflangs:
        return _check(
            i18n_hreflang_present, "info", "No hreflang alternates (multi-language sites need them)"
        )
    return _check(i18n_hreflang_present, "ok", "hreflang alternates declared", count=len(doc.hreflangs))


# ---------------------------------------------------------------------------
# security
# ---------------------------------------------------------------------------


@rule(id="security-https")
def security_https(doc: Doc, ctx: Ctx) -> Check:
    url = _url_of(doc, ctx)
    scheme = _page_scheme(url)
    if scheme == "http" and not _is_loopback(url):
        return _check(security_https, "error", "Page is served over plain HTTP")
    if scheme == "http":
        return _check(security_https, "ok", "Plain HTTP on loopback (local dev/testing)")
    if scheme == "https":
        return _check(security_https, "ok", "Page served over HTTPS")
    return _check(security_https, "ok", "Skipped (unknown scheme)")


@rule(id="security-hsts")
def security_hsts(doc: Doc, ctx: Ctx) -> Check:
    hdr = ctx.headers.get("Strict-Transport-Security", "")
    if not hdr:
        if _page_scheme(ctx.url) == "http":
            return _check(security_hsts, "warning", "No HSTS header (site is HTTP anyway)")
        return _check(security_hsts, "info", "No Strict-Transport-Security header")
    if "max-age=" not in hdr.lower():
        return _check(security_hsts, "warning", "HSTS header missing max-age")
    return _check(security_hsts, "ok", "HSTS header present")


@rule(id="security-csp")
def security_csp(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if not ctx.headers.get("Content-Security-Policy", ""):
        return _check(security_csp, "info", "No Content-Security-Policy header")
    return _check(security_csp, "ok", "CSP header present")


@rule(id="security-x-frame")
def security_x_frame(doc: Doc, ctx: Ctx) -> Check:
    hdr = ctx.headers.get("X-Frame-Options", "")
    csp = ctx.headers.get("Content-Security-Policy", "")
    if not hdr and "frame-ancestors" not in csp:
        return _check(
            security_x_frame, "info", "No clickjacking protection (X-Frame-Options or CSP frame-ancestors)"
        )
    return _check(security_x_frame, "ok", "Frame protection present")


@rule(id="security-x-content-type")
def security_x_content_type(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if not ctx.headers.get("X-Content-Type-Options", ""):
        return _check(security_x_content_type, "info", "No X-Content-Type-Options: nosniff header")
    return _check(security_x_content_type, "ok", "Content-type sniffing blocked")


@rule(id="security-referrer-policy")
def security_referrer_policy(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if not ctx.headers.get("Referrer-Policy", ""):
        return _check(security_referrer_policy, "info", "No Referrer-Policy header")
    return _check(security_referrer_policy, "ok", "Referrer policy set")


@rule(id="security-permissions-policy")
def security_permissions_policy(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if not ctx.headers.get("Permissions-Policy", ""):
        return _check(security_permissions_policy, "info", "No Permissions-Policy header")
    return _check(security_permissions_policy, "ok", "Permissions policy set")


@rule(id="security-external-links")
def security_external_links(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    blanks = [ln for ln in doc.links if ln.blank and "noopener" not in ln.rel and "noreferrer" not in ln.rel]
    if blanks:
        return _check(
            security_external_links,
            "info",
            f"{len(blanks)} target=_blank link(s) without rel=noopener",
            count=len(blanks),
        )
    return _check(security_external_links, "ok", "External links open safely")


@rule(id="security-form-https")
def security_form_https(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    insecure = [f for f in doc.forms if f.action and urlparse(f.action).scheme == "http"]
    if insecure:
        return _check(security_form_https, "error", "Form submits over plain HTTP", action=insecure[0].action)
    return _check(security_form_https, "ok", "Forms submit over HTTPS")


@rule(id="security-mixed-content")
def security_mixed_content(doc: Doc, ctx: Ctx) -> Check:
    scheme = _page_scheme(_url_of(doc, ctx))
    if scheme != "https":
        return _check(security_mixed_content, "ok", "Skipped (page is not HTTPS)")
    http_refs = [i.src for i in doc.imgs if i.src.startswith("http://")] + [
        ln.href for ln in doc.links if ln.href.startswith("http://")
    ]
    if http_refs:
        return _check(
            security_mixed_content,
            "error",
            "Mixed content: HTTPS page loads HTTP resources",
            examples=http_refs[:3],
        )
    return _check(security_mixed_content, "ok", "No mixed content")


@rule(id="security-protocol-relative")
def security_protocol_relative(doc: Doc, ctx: Ctx) -> Check:
    scheme = _page_scheme(_url_of(doc, ctx))
    if scheme != "https":
        return _check(security_protocol_relative, "ok", "Skipped (page is not HTTPS)")
    proto_rel = [i.src for i in doc.imgs if i.src.startswith("//")] + [
        ln.href for ln in doc.links if ln.href.startswith("//")
    ]
    if proto_rel:
        return _check(
            security_protocol_relative,
            "info",
            f"Protocol-relative URLs on HTTPS page ({len(proto_rel)})",
            count=len(proto_rel),
        )
    return _check(security_protocol_relative, "ok", "No protocol-relative URLs")


# ---------------------------------------------------------------------------
# technical (robots.txt / sitemap)
# ---------------------------------------------------------------------------

ROBOTS_FIELDS = (
    "user-agent",
    "allow",
    "disallow",
    "sitemap",
    "crawl-delay",
    "host",
    "clean-param",
    "request-rate",
)


def parse_robots(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """Parse robots.txt; returns (directives, invalid_lines)."""
    directives: list[dict[str, str]] = []
    invalid: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            invalid.append(raw)
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        if field not in ROBOTS_FIELDS:
            invalid.append(raw)
            continue
        directives.append({"field": field, "value": value.strip()})
    return directives, invalid


def sitemap_looks_valid(text: str) -> bool:
    """Cheap structural check: a <urlset>/<sitemapindex> root with <loc> entries."""
    head = text[:4000].lstrip()
    if not head.startswith("<?xml") and not head.startswith("<"):
        return False
    has_urlset = "<urlset" in head
    has_index = "<sitemapindex" in head
    return (has_urlset or has_index) and "<loc>" in head


@rule(id="technical-reachability")
def technical_reachability(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.status_code >= 400:
        return _check(
            technical_reachability, "error", f"Site returned HTTP {ctx.status_code}", status=ctx.status_code
        )
    return _check(
        technical_reachability, "ok", f"Site reachable (HTTP {ctx.status_code})", status=ctx.status_code
    )


@rule(id="technical-robots-txt-exists")
def technical_robots_txt_exists(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.robots is None:
        return _check(
            technical_robots_txt_exists, "warning", "robots.txt not found (crawlers may not be guided)"
        )
    return _check(technical_robots_txt_exists, "ok", "robots.txt present")


@rule(id="technical-robots-txt-valid")
def technical_robots_txt_valid(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.robots is None:
        return _check(technical_robots_txt_valid, "ok", "Skipped (no robots.txt)")
    _, invalid = parse_robots(ctx.robots)
    if invalid:
        return _check(
            technical_robots_txt_valid,
            "warning",
            f"Invalid robots.txt lines: {invalid[0]!r}",
            lines=invalid[:5],
        )
    return _check(technical_robots_txt_valid, "ok", "robots.txt parses cleanly")


@rule(id="technical-sitemap-exists")
def technical_sitemap_exists(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.sitemap is None:
        return _check(technical_sitemap_exists, "info", "Sitemap not found")
    return _check(technical_sitemap_exists, "ok", "Sitemap present")


@rule(id="technical-sitemap-valid")
def technical_sitemap_valid(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.sitemap is None:
        return _check(technical_sitemap_valid, "ok", "Skipped (no sitemap)")
    if not sitemap_looks_valid(ctx.sitemap):
        return _check(
            technical_sitemap_valid,
            "error",
            "Sitemap is not a valid XML sitemap (<urlset>/<sitemapindex> with <loc>)",
        )
    return _check(technical_sitemap_valid, "ok", "Sitemap structure valid")


@rule(id="crawl-sitemap-in-robotstxt")
def crawl_sitemap_in_robotstxt(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.robots is None:
        return _check(crawl_sitemap_in_robotstxt, "ok", "Skipped (no robots.txt)")
    directives, _ = parse_robots(ctx.robots)
    declared = [d["value"] for d in directives if d["field"] == "sitemap"]
    if not declared:
        return _check(crawl_sitemap_in_robotstxt, "info", "robots.txt does not declare a Sitemap: line")
    return _check(crawl_sitemap_in_robotstxt, "ok", "Sitemap declared in robots.txt", sitemaps=declared)


@rule(id="technical-trailing-slash")
def technical_trailing_slash(doc: Doc, ctx: Ctx) -> Check:
    url = _url_of(doc, ctx)
    if not url:
        return _check(technical_trailing_slash, "ok", "Skipped (no URL)")
    parsed = urlparse(url)
    if parsed.path and not parsed.path.endswith("/") and "." not in parsed.path.rsplit("/", 1)[-1]:
        return _check(
            technical_trailing_slash, "info", "URL path without trailing slash (pick one convention)"
        )
    return _check(technical_trailing_slash, "ok", "URL convention consistent")


@rule(id="technical-bad-content-type")
def technical_bad_content_type(doc: Doc, ctx: Ctx) -> Check:
    del doc
    ctype = ctx.headers.get("Content-Type", "")
    if ctype and "html" not in ctype.lower():
        return _check(
            technical_bad_content_type,
            "warning",
            f"Content-Type is {ctype!r}, expected text/html",
            content_type=ctype,
        )
    return _check(technical_bad_content_type, "ok", "Content-Type is HTML")


# ---------------------------------------------------------------------------
# redirects
# ---------------------------------------------------------------------------


@rule(id="redirect-meta-refresh")
def redirect_meta_refresh(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.meta_refresh:
        return _check(redirect_meta_refresh, "warning", "Meta refresh redirect detected (301/302 preferred)")
    return _check(redirect_meta_refresh, "ok", "No meta refresh redirects")


# ---------------------------------------------------------------------------
# structured data
# ---------------------------------------------------------------------------


def _parse_jsonld(doc: Doc) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for blob in doc.jsonld:
        blob = blob.strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and "@type" in item:
                items.append(item)
    return items


@rule(id="schema-present")
def schema_present(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if not doc.jsonld:
        return _check(schema_present, "info", "No JSON-LD structured data (AI engines and rich results)")
    return _check(schema_present, "ok", "JSON-LD structured data present", count=len(doc.jsonld))


@rule(id="schema-valid")
def schema_valid(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    bad = [b[:60] for b in doc.jsonld if not _jsonld_parses(b)]
    if bad:
        return _check(schema_valid, "error", f"JSON-LD fails to parse: {bad[0]!r}", sample=bad[0])
    return _check(schema_valid, "ok", "JSON-LD blocks parse", count=len(doc.jsonld))


def _jsonld_parses(blob: str) -> bool:
    try:
        json.loads(blob)
        return True
    except json.JSONDecodeError:
        return False


@rule(id="schema-required-fields")
def schema_required_fields(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    missing: list[str] = []
    for item in _parse_jsonld(doc):
        type_ = item["@type"]
        if isinstance(type_, list):
            type_ = type_[0]
        if not isinstance(type_, str):
            continue
        for required in ("name", "@id"):
            if required not in item:
                missing.append(f"{type_}:{required}")
    if missing:
        return _check(
            schema_required_fields,
            "info",
            f"Structured data missing required fields: {missing[0]}",
            fields=missing[:5],
        )
    return _check(schema_required_fields, "ok", "Structured data required fields present")


@rule(id="schema-type")
def schema_type(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    types: set[str] = set()
    for item in _parse_jsonld(doc):
        t = item["@type"]
        if isinstance(t, str):
            types.add(t)
        elif isinstance(t, list):
            types.update(t)
    if not types:
        return _check(schema_type, "ok", "Skipped (no structured data)")
    interesting = sorted(
        t
        for t in types
        if t
        in {
            "Article",
            "BlogPosting",
            "Product",
            "Organization",
            "LocalBusiness",
            "SoftwareApplication",
            "BreadcrumbList",
            "FAQPage",
            "WebSite",
            "Person",
        }
    )
    if interesting:
        return _check(schema_type, "ok", "Structured data uses recognizable types", types=sorted(types))
    return _check(schema_type, "info", "Structured data types not recognized", types=sorted(types))


# ---------------------------------------------------------------------------
# e-e-a-t
# ---------------------------------------------------------------------------


@rule(id="eeat-contact-page")
def eeat_contact_page(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    contact = [ln for ln in doc.links if ln.kind in ("mailto", "tel") or _vague_contact(ln.text)]
    if contact:
        return _check(eeat_contact_page, "ok", "Contact route found")
    return _check(eeat_contact_page, "info", "No obvious contact page link (E-E-A-T signal)")


def _vague_contact(text: str) -> bool:
    return any(k in text.lower() for k in ("contact", "联系", "联系我们"))


@rule(id="eeat-privacy-policy")
def eeat_privacy_policy(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    found = [ln for ln in doc.links if "privacy" in ln.text.lower() or "隐私" in ln.text]
    if found:
        return _check(eeat_privacy_policy, "ok", "Privacy policy linked")
    return _check(eeat_privacy_policy, "info", "No privacy policy link")


@rule(id="eeat-affiliate-disclosure")
def eeat_affiliate_disclosure(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if _text_has(doc, "affiliate", "partner link", "disclosure", "推广", "佣金"):
        return _check(eeat_affiliate_disclosure, "ok", "Affiliate disclosure found")
    return _check(eeat_affiliate_disclosure, "ok", "No affiliate disclosure needed (not detected)")


# ---------------------------------------------------------------------------
# AI/GEO readiness
# ---------------------------------------------------------------------------

AI_BOTS = (
    "chatgpt-user",
    "perplexitybot",
    "claudebot",
    "google-extended",
    "anthropic",
    "gptbot",
    "applebot",
    "bytespider",
)


@rule(id="geo-semantic-html")
def geo_semantic_html(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    semantic = doc.landmarks
    if semantic < 3:
        return _check(
            geo_semantic_html,
            "info",
            "Low use of semantic landmarks (article/main/section help AI extractors)",
        )
    return _check(geo_semantic_html, "ok", "Semantic HTML structure present")


@rule(id="geo-ai-bot-access")
def geo_ai_bot_access(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.robots is None:
        return _check(geo_ai_bot_access, "info", "Cannot assess AI-bot access (no robots.txt)")
    lower = ctx.robots.lower()
    covered = [b for b in AI_BOTS if b in lower]
    if covered:
        return _check(geo_ai_bot_access, "ok", "AI crawlers explicitly handled in robots.txt", bots=covered)
    return _check(
        geo_ai_bot_access, "info", "No explicit AI-crawler rules (ChatGPT/Perplexity/Claude) in robots.txt"
    )


# ---------------------------------------------------------------------------
# legal
# ---------------------------------------------------------------------------


@rule(id="legal-cookie-consent")
def legal_cookie_consent(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if _text_has(doc, "cookie", "consent", "隐私设置"):
        return _check(legal_cookie_consent, "ok", "Cookie/consent notice detected")
    return _check(legal_cookie_consent, "info", "No cookie consent notice detected")


# ---------------------------------------------------------------------------
# performance (static hints + response headers)
# ---------------------------------------------------------------------------


@rule(id="perf-response-time")
def perf_response_time(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.elapsed_ms > 3000:
        return _check(
            perf_response_time,
            "info",
            f"Slow response ({ctx.elapsed_ms}ms; aim under 3000ms)",
            ms=ctx.elapsed_ms,
        )
    return _check(perf_response_time, "ok", f"Response time {ctx.elapsed_ms}ms", ms=ctx.elapsed_ms)


@rule(id="perf-page-weight")
def perf_page_weight(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if ctx.byte_size > 1_000_000:
        return _check(
            perf_page_weight,
            "warning",
            f"Heavy HTML page ({ctx.byte_size // 1024}KB; aim under 1MB)",
            bytes=ctx.byte_size,
        )
    return _check(perf_page_weight, "ok", f"Page weight {ctx.byte_size // 1024}KB", bytes=ctx.byte_size)


@rule(id="perf-dom-size")
def perf_dom_size(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.element_count > 1500:
        return _check(
            perf_dom_size,
            "info",
            f"Large DOM ({doc.element_count} elements; keep under ~1500)",
            count=doc.element_count,
        )
    return _check(perf_dom_size, "ok", f"DOM size {doc.element_count} elements", count=doc.element_count)


@rule(id="perf-preconnect")
def perf_preconnect(doc: Doc, ctx: Ctx) -> Check:
    del ctx
    if doc.preconnects == 0 and doc.links:
        return _check(perf_preconnect, "info", "No preconnect/preload resource hints")
    return _check(perf_preconnect, "ok", "Resource hints present", count=doc.preconnects)


@rule(id="perf-text-compression")
def perf_text_compression(doc: Doc, ctx: Ctx) -> Check:
    del doc
    enc = (ctx.headers.get("Content-Encoding", "") or "").lower()
    if not enc:
        return _check(perf_text_compression, "info", "No content encoding (enable gzip/brotli)")
    return _check(perf_text_compression, "ok", "Content compressed", encoding=enc)


@rule(id="perf-http2")
def perf_http2(doc: Doc, ctx: Ctx) -> Check:
    del doc
    alt_svc = ctx.headers.get("Alt-Svc", "")
    if not alt_svc and "HTTP/2" not in str(ctx.headers.get("X-Protocol", "")):
        return _check(perf_http2, "info", "No HTTP/2 signal detected (Alt-Svc header)")
    return _check(perf_http2, "ok", "HTTP/2 signalled")


@rule(id="perf-cache-policy")
def perf_cache_policy(doc: Doc, ctx: Ctx) -> Check:
    del doc
    if not ctx.headers.get("Cache-Control", "") and not ctx.headers.get("Expires", ""):
        return _check(perf_cache_policy, "info", "No Cache-Control header")
    return _check(perf_cache_policy, "ok", "Cache policy set")
