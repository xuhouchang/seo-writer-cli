"""Real, user-configured provider adapters.

These adapters use only standard-library HTTP. Credentials are loaded from the
user-owned data directory at runtime and never enter ProviderResult, audit
payloads or error messages. Tests redirect every endpoint to a local HTTP stub.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from ..errors import PermanentProviderError, TransientProviderError, UsageError
from .base import ProviderResult


def _fingerprint(operation: str, payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{operation}:{encoded}".encode()).hexdigest()


def _credentials(data_dir: Path, provider: str, fields: tuple[str, ...]) -> dict[str, str]:
    path = Path(data_dir) / ".secrets.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise UsageError(
            f"provider '{provider}' is not configured; run "
            f"`seo-writer providers configure --name {provider}`"
        ) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise UsageError(f"cannot read provider credentials at {path}: {exc}") from exc
    values = payload.get(provider) if isinstance(payload, dict) else None
    if not isinstance(values, dict) or any(not str(values.get(field) or "").strip() for field in fields):
        names = ", ".join(fields)
        raise UsageError(
            f"provider '{provider}' is not configured; run `seo-writer providers configure --name {provider}`"
            f" (required: {names})"
        )
    return {field: str(values[field]) for field in fields}


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any] | list[Any]]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"message": f"HTTP {exc.code}"}
        if exc.code == 429 or exc.code >= 500:
            raise TransientProviderError("http", f"HTTP {exc.code}") from exc
        raise PermanentProviderError("http", f"HTTP {exc.code}: {_safe_message(payload)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientProviderError("http", f"network error: {exc.__class__.__name__}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermanentProviderError("http", "provider returned invalid JSON") from exc


def _safe_message(payload: object) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("status_message") or "provider request failed")[:240]
    return "provider request failed"


def _dataforseo_result(payload: dict[str, Any], provider: str, operation: str) -> tuple[list[Any], float]:
    if payload.get("status_code") != 20000:
        code = payload.get("status_code")
        message = str(payload.get("status_message") or "request failed")[:240]
        if code in {40100, 40200, 40201, 40400, 40500}:
            raise PermanentProviderError(provider, f"{operation} failed ({code}): {message}")
        raise TransientProviderError(provider, f"{operation} failed ({code}): {message}")
    tasks = payload.get("tasks") or []
    if not tasks:
        raise PermanentProviderError(provider, f"{operation} returned no task result")
    task = tasks[0]
    if task.get("status_code") != 20000:
        raise PermanentProviderError(provider, f"{operation} task failed: {_safe_message(task)}")
    return list(task.get("result") or []), float(task.get("cost") or payload.get("cost") or 0.0)


class DataForSEOProvider:
    """DataForSEO keyword-data and Google organic SERP live adapter."""

    name = "dataforseo"

    def __init__(
        self,
        profile: str,
        data_dir: Path,
        *,
        base_url: str = "https://api.dataforseo.com",
        location_code: int = 2840,
        language_code: str = "en",
        device: str = "desktop",
    ) -> None:
        credentials = _credentials(data_dir, "dataforseo", ("login", "password"))
        self.profile = profile
        self.login = credentials["login"]
        self.password = credentials["password"]
        self.base_url = base_url.rstrip("/")
        self.location_code = location_code
        self.language_code = language_code
        self.device = device

    def _post(self, path: str, operation: str, task: dict[str, Any]) -> tuple[list[Any], float]:
        auth = base64.b64encode(f"{self.login}:{self.password}".encode()).decode()
        url = f"{self.base_url}{path}"
        body = json.dumps([task]).encode("utf-8")
        status, payload = _http_json(
            "POST",
            url,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
        )
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            raise PermanentProviderError(self.name, f"{operation} returned an invalid response")
        return _dataforseo_result(payload, self.name, operation)

    def _task(self, keywords: list[str]) -> dict[str, Any]:
        return {
            "keywords": keywords,
            "location_code": self.location_code,
            "language_code": self.language_code,
        }

    def search_volume(self, keywords: list[str]) -> ProviderResult:
        task = self._task(keywords)
        rows, cost = self._post("/v3/keywords_data/google/search_volume/live", "keyword.search_volume", task)
        data = {
            str(row.get("keyword")): {
                "volume": row.get("search_volume"),
                "difficulty": row.get("competition"),
                "cpc": row.get("cpc"),
            }
            for row in rows
            if row.get("keyword")
        }
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="keyword.search_volume",
            data=data,
            request_fingerprint=_fingerprint("keyword.search_volume", task),
            cost_estimate=cost,
            source_confidence="dataforseo",
        )

    def related(self, keyword: str) -> ProviderResult:
        task = {**self._task([keyword]), "limit": 100}
        rows, cost = self._post(
            "/v3/keywords_data/google/keyword_suggestions/live", "keyword.related", task
        )
        data = [str(row.get("keyword")) for row in rows if row.get("keyword")]
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="keyword.related",
            data=data,
            request_fingerprint=_fingerprint("keyword.related", task),
            cost_estimate=cost,
            source_confidence="dataforseo",
        )

    def paa(self, keyword: str) -> ProviderResult:
        result = self.query(keyword, "head")
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="keyword.paa",
            data=result.data.get("paa", []),
            request_fingerprint=_fingerprint("keyword.paa", keyword),
            cost_estimate=result.cost_estimate,
            source_confidence="dataforseo",
        )

    def query(self, keyword: str, variant: str) -> ProviderResult:
        prefixes = {"head": "", "howto": "how to ", "decision": ""}
        suffixes = {"head": "", "howto": "", "decision": " vs alternatives why it fails"}
        query = f"{prefixes.get(variant, '')}{keyword}{suffixes.get(variant, '')}".strip()
        task = {
            "keyword": query,
            "location_code": self.location_code,
            "language_code": self.language_code,
            "device": self.device,
            "depth": 10,
        }
        rows, cost = self._post("/v3/serp/google/organic/live/advanced", "serp.query", task)
        result = rows[0] if rows else {}
        items = result.get("items") or []
        organic: list[dict[str, Any]] = []
        paa: list[str] = []
        related: list[str] = []
        aio_visible = False
        for item in items:
            item_type = item.get("type") or ""
            if item_type in {"organic", "organic_paid"} and item.get("url"):
                organic.append(
                    {
                        "rank": item.get("rank_group") or item.get("rank_absolute"),
                        "url": item["url"],
                        "title": item.get("title") or item["url"],
                        "type": item_type,
                    }
                )
            if item_type in {"people_also_ask", "people_also_ask_element"}:
                for question in item.get("items") or [item]:
                    if question.get("title"):
                        paa.append(str(question["title"]))
            if item_type in {"related_searches", "related_searches_element"}:
                for entry in item.get("items") or [item]:
                    if entry.get("title"):
                        related.append(str(entry["title"]))
            if "ai_overview" in item_type:
                aio_visible = True
        data = {
            "query": query,
            "variant": variant,
            "query_method": "dataforseo_api",
            "aio_visible": aio_visible,
            "aio_conclusion": "observed" if aio_visible else "absent in response",
            "paa": paa,
            "related_searches": related,
            "organic": organic,
        }
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="serp.query",
            data=data,
            request_fingerprint=_fingerprint("serp.query", task),
            cost_estimate=cost,
            source_confidence="dataforseo",
        )


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.headings: list[str] = []
        self.paragraphs: list[str] = []
        self._tag = ""
        self._buffer: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif not self._skip and (tag == "title" or tag in {"h1", "h2", "h3"} or tag == "p" ):
            self._tag = tag
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        elif tag == self._tag:
            text = re.sub(r"\s+", " ", html.unescape("".join(self._buffer))).strip()
            if text:
                if tag == "title":
                    self.title = text
                elif tag.startswith("h"):
                    self.headings.append(text)
                elif tag == "p":
                    self.paragraphs.append(text)
            self._tag = ""
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if not self._skip and self._tag:
            self._buffer.append(data)


class HttpWebFetchProvider:
    """Fetch and extract page evidence without storing raw HTML in results."""

    name = "http"

    def __init__(self, profile: str = "real-http", timeout: float = 20.0) -> None:
        self.profile = profile
        self.timeout = timeout

    def fetch(self, url: str) -> ProviderResult:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "seo-writer/0.1 (+local research)", "Accept": "text/html"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(5 * 1024 * 1024)
                status = response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise TransientProviderError(self.name, f"HTTP {exc.code}") from exc
            raise PermanentProviderError(self.name, f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientProviderError(self.name, f"network error: {exc.__class__.__name__}") from exc
        parser = _PageParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="webfetch.fetch",
            data={
                "url": url,
                "title": parser.title or url,
                "headings": parser.headings[:20],
                "main_claims": parser.paragraphs[:5],
                "limitations": ["page content is customer-visible source material; claims require review"],
                "content_type": "html",
                "fetch_method": "web_fetch",
                "status_code": status,
            },
            request_fingerprint=_fingerprint("webfetch.fetch", {"url": url}),
            source_confidence="real_page",
        )


class RedditProvider:
    """Reddit OAuth search plus Stack Exchange second-platform evidence."""

    name = "reddit"

    def __init__(
        self,
        profile: str,
        data_dir: Path,
        *,
        oauth_base_url: str = "https://oauth.reddit.com",
        token_url: str = "https://www.reddit.com/api/v1/access_token",
        stackexchange_base_url: str = "https://api.stackexchange.com",
    ) -> None:
        credentials = _credentials(data_dir, "reddit", ("client_id", "client_secret"))
        self.profile = profile
        self.client_id = credentials["client_id"]
        self.client_secret = credentials["client_secret"]
        self.oauth_base_url = oauth_base_url.rstrip("/")
        self.token_url = token_url
        self.stackexchange_base_url = stackexchange_base_url.rstrip("/")
        self._access_token: str | None = None
        self._last_query = ""

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        status, payload = _http_json(
            "POST",
            self.token_url,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "seo-writer/0.1",
            },
            body=body,
        )
        if status < 200 or status >= 300 or not isinstance(payload, dict) or not payload.get("access_token"):
            raise PermanentProviderError(self.name, "OAuth token response did not contain an access token")
        self._access_token = str(payload["access_token"])
        return self._access_token

    def _get_reddit(self, path: str, params: dict[str, Any]) -> Any:
        query = urllib.parse.urlencode(params)
        status, payload = _http_json(
            "GET",
            f"{self.oauth_base_url}{path}?{query}",
            headers={"Authorization": f"Bearer {self._token()}", "User-Agent": "seo-writer/0.1"},
        )
        if status < 200 or status >= 300:
            raise PermanentProviderError(self.name, f"Reddit request failed ({status})")
        return payload

    def discover(self, query: str) -> ProviderResult:
        self._last_query = query
        payload = self._get_reddit(
            "/search.json", {"q": query, "sort": "relevance", "t": "year", "limit": 25, "raw_json": 1}
        )
        children = payload.get("data", {}).get("children", []) if isinstance(payload, dict) else []
        data = []
        for child in children:
            item = child.get("data", {})
            permalink = item.get("permalink") or ""
            if not permalink:
                continue
            subreddit = item.get("subreddit") or "unknown"
            data.append(
                {
                    "url": urllib.parse.urljoin("https://www.reddit.com", permalink),
                    "platform": f"Reddit / r/{subreddit}",
                    "title": item.get("title") or "Reddit discussion",
                    "snippet_only": True,
                }
            )
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="community.discover",
            data=data,
            request_fingerprint=_fingerprint("community.discover", {"query": query}),
            source_confidence="reddit_api",
        )

    def read_thread(self, url: str, platform: str) -> ProviderResult:
        parsed = urllib.parse.urlparse(url)
        match = re.search(r"/comments/([A-Za-z0-9]+)", parsed.path)
        if not match:
            raise PermanentProviderError(self.name, "Reddit thread URL has no post id")
        payload = self._get_reddit(f"/comments/{match.group(1)}.json", {"raw_json": 1, "limit": 20})
        post = (
            ((payload[0] if isinstance(payload, list) else {}).get("data") or {}).get("children") or [{}]
        )[0]
        item = post.get("data", {}) if isinstance(post, dict) else {}
        comments = (
            (
                (payload[1] if isinstance(payload, list) and len(payload) > 1 else {}).get("data") or {}
            ).get("children")
            or []
        )
        bodies = [str(c.get("data", {}).get("body") or "") for c in comments if c.get("kind") == "t1"]
        body = str(item.get("selftext") or "").strip()
        promotional = bool(
            re.search(
                r"\b(buy|discount|sponsor|affiliate|promo code)\b",
                f"{body} {item.get('title', '')}",
                re.I,
            )
        )
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="community.read_thread",
            data={
                "url": url,
                "platform": platform,
                "title": item.get("title") or url,
                "original_post_summary": body[:1200] or "Reddit post has no text body",
                "high_signal_comments_read": len(bodies),
                "counterexample": next(
                    (comment[:500] for comment in bodies if comment),
                    "no counterexample captured",
                ),
                "promotion_disclosure": "promotional" if promotional else "none",
                "grade": "promotional" if promotional else "high",
                "fetch_method": "reddit_api",
            },
            request_fingerprint=_fingerprint("community.read_thread", {"url": url}),
            source_confidence="reddit_api",
        )

    def second_platform_thread(self) -> ProviderResult:
        query = self._last_query or "SEO workflow"
        params = urllib.parse.urlencode(
            {"order": "desc", "sort": "relevance", "q": query, "site": "stackoverflow", "pagesize": 10}
        )
        status, payload = _http_json(
            "GET",
            f"{self.stackexchange_base_url}/2.3/search/advanced?{params}",
            headers={"User-Agent": "seo-writer/0.1"},
        )
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            raise PermanentProviderError(self.name, "second-platform search returned an invalid response")
        item = (payload.get("items") or [{}])[0]
        url = item.get("link") or "https://stackoverflow.com/"
        title = html.unescape(str(item.get("title") or "Stack Exchange discussion"))
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="community.read_thread",
            data={
                "url": url,
                "platform": "Stack Exchange / Stack Overflow",
                "title": title,
                "original_post_summary": re.sub(
                    r"<[^>]+>", " ", str(item.get("body_markdown") or title)
                )[:1200],
                "high_signal_comments_read": 0,
                "counterexample": "comments were not requested from the second platform",
                "promotion_disclosure": "none",
                "grade": "high",
                "fetch_method": "stackexchange_api",
            },
            request_fingerprint=_fingerprint("community.second_platform", {"query": query}),
            source_confidence="stackexchange_api",
        )
