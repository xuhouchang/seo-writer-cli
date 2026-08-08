"""Google Search Console integration: credentials, OAuth, pull, insights.

Closes the loop production → publish → measure → iterate. Everything uses
only the standard library (urllib / hashlib / base64 / http.server / csv /
sqlite3) — no google-api-python-client, no requests, no google-auth-oauthlib.

Credentials and data stay on the customer's machine: the gcloud ADC file is
read in place, a self-built client writes a chmod-600 client json + token
file under the workspace. Secrets never enter git, logs, or error messages —
audit events only carry property references and file paths.

Tests run against synthetic credentials and local stub endpoints; real
customer data never appears in fixtures. Endpoint URLs can be redirected
through SEO_WRITER_GSC_* environment variables (used by the CLI test
harness); functions also accept explicit URLs for direct unit tests.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from .config import Workspace
from .db import Database
from .errors import SeoWriterError, UsageError
from .ids import utcnow

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GCLOUD_ADC_PATH = Path(
    os.environ.get("SEO_WRITER_GSC_ADC_PATH")
    or Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
)
TOKEN_URL = os.environ.get("SEO_WRITER_GSC_TOKEN_URL") or "https://oauth2.googleapis.com/token"
TOKENINFO_URL = os.environ.get("SEO_WRITER_GSC_TOKENINFO_URL") or "https://oauth2.googleapis.com/tokeninfo"
AUTH_URL = os.environ.get("SEO_WRITER_GSC_AUTH_URL") or "https://accounts.google.com/o/oauth2/v2/auth"
API_BASE = os.environ.get("SEO_WRITER_GSC_API_BASE") or "https://searchconsole.googleapis.com"
GCLOUD_BIN = "gcloud"
ROW_LIMIT = 25_000
DEFAULT_PULL_DAYS = 30
FRESHNESS_DELAY_DAYS = 3
AUTH_TIMEOUT_S = 300.0
MAX_PULL_DAYS = 500  # covers the 16-month API window plus slack

COVERAGE_LABELS = {
    "SUBMITTED_AND_INDEXED": "Submitted and indexed",
    "SUBMITTED_BUT_NOT_INDEXED": "Submitted, not indexed",
    "CRAWLED_AND_CURRENTLY_NOT_INDEXED": "Crawled, currently not indexed",
    "DUPLICATE_GOOGLE_CHOSEN_CANONICAL": "Duplicate: Google chose a different canonical",
    "DUPLICATE_USER_CHOSEN_CANONICAL": "Duplicate: submitted URL is not the canonical",
    "PAGE_WITH_REDIRECT": "Page with redirect",
    "PAGE_WITH_SOFT_404": "Page with soft 404",
    "NOT_FOUND": "Not found",
    "NOT_CRAWLABLE": "Not crawlable",
    "CRAWL_ANOMALY": "Crawl anomaly",
    "SERVER_ERROR": "Server error",
    "UNSPECIFIED": "Unspecified",
}


# ---------------------------------------------------------------------------
# error taxonomy
# ---------------------------------------------------------------------------


class GscError(SeoWriterError):
    """GSC integration failure; retryable errors are safe to back off on."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


class GscAuthError(GscError):
    """Permanent auth failure: revoked token, bad client, missing scope."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class GscQuotaError(GscError):
    """Quota-limited (429 / quota 403); retryable with backoff."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class GscTransientError(GscError):
    """Network / 5xx failure; retryable with backoff."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    refresh_token: str | None
    auth_type: str  # 'gcloud-adc' | 'own-client'
    client_json_path: str | None = None
    token_file: str | None = None
    quota_project_id: str | None = None


def gsc_brand_dir(ws: Workspace, brand: str) -> Path:
    return ws.root / "gsc" / brand


def client_json_path(ws: Workspace, brand: str) -> Path:
    return gsc_brand_dir(ws, brand) / "client.json"


def token_file_path(ws: Workspace, brand: str) -> Path:
    return gsc_brand_dir(ws, brand) / "token.json"


def load_adc(path: Path | None = None) -> Credentials:
    """Read a gcloud application_default_credentials.json (authorized_user)."""
    adc = Path(path) if path is not None else GCLOUD_ADC_PATH
    if not adc.exists():
        raise GscError(
            f"gcloud ADC credentials not found at {adc}. "
            f'Run: gcloud auth application-default login --scopes="{SCOPE}"'
        )
    try:
        payload = json.loads(adc.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GscError(f"cannot read gcloud ADC file {adc}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "authorized_user":
        raise GscError(
            f"{adc} is not an authorized_user credential (type={payload.get('type')!r}); "
            f're-run gcloud auth application-default login --scopes="{SCOPE}"'
        )
    client_id = str(payload.get("client_id") or "")
    client_secret = str(payload.get("client_secret") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    if not client_id or not client_secret or not refresh_token:
        raise GscError(
            f"{adc} is missing client_id/client_secret/refresh_token; "
            f're-run gcloud auth application-default login --scopes="{SCOPE}"'
        )
    quota_project_id = payload.get("quota_project_id")
    if not isinstance(quota_project_id, str) or not quota_project_id:
        quota_project_id = None
    return Credentials(
        client_id,
        client_secret,
        refresh_token,
        "gcloud-adc",
        client_json_path=str(adc),
        quota_project_id=quota_project_id,
    )


def _parse_client_json(path: Path) -> dict[str, str]:
    """Validate a Google Desktop-app client json; returns client_id/secret."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GscError(f"cannot read client json {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GscError(f"{path} is not a JSON object — download the Desktop app client json")
    section = payload.get("installed") or payload.get("web")
    if not isinstance(section, dict):
        raise GscError(
            f"{path} has no installed/web section — download the Desktop app client json from "
            "console.cloud.google.com/apis/credentials"
        )
    client_id = str(section.get("client_id") or "")
    client_secret = str(section.get("client_secret") or "")
    if not client_id or not client_secret:
        raise GscError(
            f"{path} is missing client_id/client_secret — download the Desktop app client json from "
            "console.cloud.google.com/apis/credentials"
        )
    return {"client_id": client_id, "client_secret": client_secret}


def import_client_json(ws: Workspace, brand: str, src: str | Path) -> dict[str, Any]:
    """Copy + validate a Desktop-app client json into the workspace (chmod-600)."""
    source = Path(src)
    if not source.exists():
        raise UsageError(f"client json not found: {source}")
    _parse_client_json(source)
    target = client_json_path(ws, brand)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    target.chmod(0o600)
    return {
        "brand": brand,
        "auth_path": "own-client",
        "client_json": str(target),
        "status": "client-imported",
        "next": f"run `seo-writer gsc auth --brand {brand}` to authorize once",
    }


def load_client_json(ws: Workspace, brand: str) -> Credentials:
    path = client_json_path(ws, brand)
    if not path.exists():
        raise GscError(
            f"brand '{brand}' has no own client json; "
            f"run `seo-writer gsc setup --brand {brand} --client-json <path>` first"
        )
    fields = _parse_client_json(path)
    return Credentials(
        fields["client_id"],
        fields["client_secret"],
        load_token(ws, brand),
        "own-client",
        client_json_path=str(path),
        token_file=str(token_file_path(ws, brand)),
    )


def save_token(ws: Workspace, brand: str, refresh_token: str) -> Path:
    path = token_file_path(ws, brand)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"refresh_token": refresh_token}), encoding="utf-8")
    path.chmod(0o600)
    return path


def load_token(ws: Workspace, brand: str) -> str | None:
    path = token_file_path(ws, brand)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return str(payload.get("refresh_token")) if isinstance(payload, dict) else None


def load_credentials(db: Database, ws: Workspace, brand: str) -> tuple[Credentials, dict[str, Any] | None]:
    """Resolve the brand's credentials: connected auth path, else ADC, else own client."""
    prop = db.get_gsc_property(brand)
    auth_path = (prop or {}).get("auth_path")
    if auth_path == "own-client":
        return load_client_json(ws, brand), prop
    if auth_path == "gcloud-adc":
        return load_adc(), prop
    if GCLOUD_ADC_PATH.exists():
        return load_adc(), prop
    if client_json_path(ws, brand).exists():
        return load_client_json(ws, brand), prop
    raise GscError(
        f"brand '{brand}' has no GSC credentials configured; "
        f"run `seo-writer gsc setup --brand {brand}` first"
    )


# ---------------------------------------------------------------------------
# token refresh + scope verification
# ---------------------------------------------------------------------------


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GscTransientError(f"network error for {url}: {exc}") from exc


def _google_error_message(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")[:300]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            bits = [str(err.get("status") or ""), str(err.get("message") or "")]
            return ": ".join(b for b in bits if b) or str(err)
        if err:
            return f"{err}: {payload.get('error_description') or ''}".strip(":")
        return json.dumps(payload, ensure_ascii=False)[:300]
    return str(payload)[:300]


def _raise_for_status(status: int, body: bytes, context: str) -> None:
    if 200 <= status < 300:
        return
    message = _google_error_message(body) or f"HTTP {status}"
    if status == 429:
        raise GscQuotaError(f"{context} quota exceeded (429): {message}")
    if status in (401, 403):
        if "insufficientPermissions" in message:
            raise GscAuthError(
                f"{context} permission denied: {message}. Re-run `gsc auth` to authorize the account."
            )
        if "quota" in message.lower():
            raise GscQuotaError(f"{context} quota exceeded (403): {message}")
        raise GscAuthError(
            f"{context} authentication failed (HTTP {status}): {message}. Re-run `gsc auth`."
        )
    if status >= 500:
        raise GscTransientError(f"{context} server error (HTTP {status}): {message}")
    raise GscError(f"{context} request failed (HTTP {status}): {message}")


def refresh_access_token(
    creds: Credentials, *, token_url: str | None = None, timeout: float = 30.0
) -> str:
    """Exchange the refresh token for an access token (standard library only)."""
    if not creds.refresh_token:
        raise GscAuthError(
            f"{creds.auth_type} credentials carry no refresh_token; re-run `gsc auth`"
        )
    token_url = token_url or TOKEN_URL
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
        }
    ).encode("utf-8")
    status, body = _http_request(
        "POST",
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=data,
        timeout=timeout,
    )
    if status != 200:
        message = _google_error_message(body)
        if status == 400 and "invalid_grant" in message:
            raise GscAuthError(
                f"refresh token was revoked or expired ({message}); re-run `gsc auth`."
            )
        if status == 400 and "invalid_client" in message:
            raise GscAuthError(
                f"client credentials were rejected ({message}); "
                "re-import the Desktop-app client json via `gsc setup --client-json <path>`."
            )
        if status == 400 and "invalid_scope" in message:
            raise GscAuthError(
                f"the webmasters scope is missing ({message}); re-run `gsc auth`."
            )
        _raise_for_status(status, body, "token refresh")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GscError("token endpoint returned a non-JSON response") from exc
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise GscAuthError("token endpoint returned no access_token")
    return token


def check_scope(
    access_token: str, *, tokeninfo_url: str | None = None, timeout: float = 30.0
) -> dict[str, Any]:
    """Ask Google which scopes the access token carries (no secrets echoed)."""
    tokeninfo_url = tokeninfo_url or TOKENINFO_URL
    status, body = _http_request(
        "GET",
        f"{tokeninfo_url}?access_token={urllib.parse.quote(access_token, safe='')}",
        timeout=timeout,
    )
    if status != 200:
        _raise_for_status(status, body, "scope check")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GscError("tokeninfo endpoint returned a non-JSON response") from exc
    scopes = str(payload.get("scope") or "").split()
    return {"scopes": scopes, "has_webmasters": any("webmasters" in s for s in scopes)}


def verify_credentials(
    creds: Credentials, *, token_url: str | None = None, tokeninfo_url: str | None = None
) -> dict[str, Any]:
    """Refresh + scope check; raises GscAuthError when the refresh fails."""
    token = refresh_access_token(creds, token_url=token_url)
    scope = check_scope(token, tokeninfo_url=tokeninfo_url)
    if scope["has_webmasters"]:
        return {"ok": True, "has_webmasters": True, "message": "credentials work; webmasters scope granted"}
    return {
        "ok": False,
        "has_webmasters": False,
        "message": f"credentials work but the webmasters scope is missing; "
        f're-run the auth command with --scopes="{SCOPE}"',
    }


# ---------------------------------------------------------------------------
# backoff + rate limiter
# ---------------------------------------------------------------------------


def _jittered_delay(attempt: int, base: float, max_delay: float) -> float:
    delay = min(max_delay, base * (2**attempt))
    # deterministic pseudo-jitter (keeps tests reproducible)
    wobble = 0.9 + 0.2 * ((attempt * 7919) % 10) / 10
    return round(delay * wobble, 3)


def with_backoff(
    fn: Callable[..., Any],
    *args: Any,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """Run fn with exponential backoff on quota/transient errors (1s→60s + jitter)."""
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (GscQuotaError, GscTransientError):
            if attempt == attempts - 1:
                raise
            sleep(_jittered_delay(attempt, base_delay, max_delay))
    raise GscError("backoff loop exhausted without a result")  # unreachable


class RateLimiter:
    """Pace requests to at most one per min_interval (defensive 1,200 QPM cap)."""

    def __init__(
        self,
        min_interval: float = 60.0 / 1200.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_interval = min_interval
        self._sleep = sleep
        self._now = now
        self._last: float = 0.0

    def wait(self) -> None:
        elapsed = self._now() - self._last
        if self._last and elapsed < self._min_interval:
            self._sleep(self._min_interval - elapsed)
        self._last = self._now()


# ---------------------------------------------------------------------------
# API transport
# ---------------------------------------------------------------------------


def _api_request(
    method: str,
    url: str,
    *,
    access_token: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
    quota_project_id: str | None = None,
) -> dict[str, Any] | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    # user (ADC) credentials need an explicit quota project on this API;
    # gcloud stores it in the ADC file as quota_project_id
    if quota_project_id:
        headers["X-Goog-User-Project"] = quota_project_id
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    status, raw = _http_request(method, url, headers=headers, body=data, timeout=timeout)
    _raise_for_status(status, raw, url)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GscError(f"{url} returned a non-JSON response") from exc


def _require_property(prop: dict[str, Any] | None, brand: str) -> str:
    if prop is None or not prop.get("property_url"):
        raise GscError(
            f"brand '{brand}' has no connected GSC property; "
            f"run `seo-writer gsc connect --brand {brand} --property <url>`"
        )
    return str(prop["property_url"])


def _validate_property(property_url: str) -> str:
    value = property_url.strip()
    if value.startswith("sc-domain:"):
        domain = value[len("sc-domain:") :]
        if not domain or "/" in domain or " " in domain:
            raise UsageError(f"invalid property '{property_url}': expected sc-domain:<domain>")
        return value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError(f"invalid property '{property_url}': expected https://… or sc-domain:…")
    return value


# ---------------------------------------------------------------------------
# sites / connect / pull
# ---------------------------------------------------------------------------


def list_sites(creds: Credentials, *, api_base: str | None = None) -> dict[str, Any]:
    api_base = api_base or API_BASE
    token = refresh_access_token(creds)
    payload = (
        _api_request(
            "GET",
            f"{api_base}/webmasters/v3/sites",
            access_token=token,
            quota_project_id=creds.quota_project_id,
        )
        or {}
    )
    entries = payload.get("siteEntry") or []
    sites = sorted(
        (
            {"site_url": e.get("siteUrl"), "permission_level": e.get("permissionLevel")}
            for e in entries
            if e.get("siteUrl")
        ),
        key=lambda s: s["site_url"],
    )
    return {"sites": sites}


def connect_property(db: Database, ws: Workspace, brand: str, property_url: str) -> dict[str, Any]:
    """Bind a GSC property to the brand (verified against the account's sites)."""
    property_url = _validate_property(property_url)
    creds, _ = load_credentials(db, ws, brand)
    available = [s["site_url"] for s in list_sites(creds)["sites"]]
    if property_url not in available:
        raise GscError(
            f"property {property_url} is not in this account's GSC properties; "
            f"run `seo-writer gsc sites --brand {brand}` to list what is available"
        )
    row = db.upsert_gsc_property(brand, property_url, creds.auth_type, creds.client_json_path)
    db.add_audit(
        None,
        "gsc.connect",
        {"brand": brand, "property": property_url, "auth_path": creds.auth_type},
    )
    return {
        "brand": brand,
        "property": property_url,
        "auth_path": creds.auth_type,
        "client_json_path": creds.client_json_path,
        "status": row["status"],
        "next": f"run `seo-writer gsc pull --brand {brand}` to pull search analytics",
    }


def _date_range(start: str, end: str) -> list[str]:
    try:
        start_dt = date.fromisoformat(start)
        end_dt = date.fromisoformat(end)
    except ValueError as exc:
        raise UsageError(f"invalid date '{exc.args[0]}': expected YYYY-MM-DD") from exc
    if start_dt > end_dt:
        raise UsageError(f"start date {start} is after end date {end}")
    days = []
    current = start_dt
    while current <= end_dt:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def default_pull_window(*, today: date | None = None) -> tuple[str, str]:
    """Default window: last DEFAULT_PULL_DAYS days, minus the 3-day freshness delay."""
    end = (today or date.today()) - timedelta(days=FRESHNESS_DELAY_DAYS)
    start = end - timedelta(days=DEFAULT_PULL_DAYS - 1)
    return start.isoformat(), end.isoformat()


def _query_page(
    api_base: str,
    access_token: str,
    property_url: str,
    day: str,
    dimensions: list[str],
    start_row: int,
    quota_project_id: str | None = None,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    body = {
        "startDate": day,
        "endDate": day,
        "dimensions": dimensions,
        "rowLimit": ROW_LIMIT,
        "startRow": start_row,
    }
    url = (
        f"{api_base}/webmasters/v3/sites/{urllib.parse.quote(property_url, safe='')}"
        "/searchAnalytics/query"
    )
    return (
        _api_request(
            "POST",
            url,
            access_token=access_token,
            body=body,
            timeout=timeout,
            quota_project_id=quota_project_id,
        )
        or {}
    )


def _api_rows_to_db_rows(
    property_url: str, data_date: str, dimensions: list[str], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        keys = row.get("keys") or []
        dim_key = str(keys[1]) if len(keys) > 1 else ""
        out.append(
            {
                "property_url": property_url,
                "data_date": data_date,
                "query": dim_key if "query" in dimensions else "",
                "page": dim_key if "page" in dimensions else "",
                "device": None,
                "country": None,
                "search_type": "web",
                "clicks": int(row.get("clicks") or 0),
                "impressions": int(row.get("impressions") or 0),
                "ctr": float(row.get("ctr") or 0),
                "position": float(row.get("position") or 0),
                "pulled_at": utcnow(),
            }
        )
    return out


def pull_search_analytics(
    db: Database,
    ws: Workspace,
    brand: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
    api_base: str | None = None,
    attempts: int = 4,
    limiter: RateLimiter | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Pull (date,query) + (date,page) rows per day; idempotent via gsc_pull_state.

    Already-completed (property, dimension, date) chunks are skipped unless
    --force. Each page respects the rate limiter; quota/transient errors back
    off exponentially and stop with exit 1 once attempts are exhausted.
    """
    api_base = api_base or API_BASE
    creds, prop = load_credentials(db, ws, brand)
    property_url = _require_property(prop, brand)
    start = start_date or default_pull_window()[0]
    end = end_date or default_pull_window()[1]
    dates = _date_range(start, end)
    if len(dates) > MAX_PULL_DAYS:
        raise UsageError(
            f"date range too large ({len(dates)} days): at most {MAX_PULL_DAYS} days per pull"
        )
    limiter = limiter or RateLimiter()
    dims = [("date", "query"), ("date", "page")]
    stats: dict[str, Any] = {
        "brand": brand,
        "property": property_url,
        "start_date": start,
        "end_date": end,
        "dimensions": [",".join(d) for d in dims],
        "dates_total": len(dates),
        "dates_skipped": 0,
        "dates_pulled": 0,
        "rows_written": 0,
        "api_calls": 0,
    }
    token: str | None = None
    for dimensions in dims:
        dim = ",".join(dimensions)
        for day in dates:
            if not force and db.gsc_pull_complete(property_url, dim, day):
                stats["dates_skipped"] += 1
                continue
            stats["dates_pulled"] += 1
            if token is None:  # lazy refresh: a fully-skipped run makes zero HTTP calls
                token = refresh_access_token(creds)
            start_row = 0
            while True:
                limiter.wait()
                payload = with_backoff(
                    _query_page,
                    api_base,
                    token,
                    property_url,
                    day,
                    list(dimensions),
                    start_row,
                    creds.quota_project_id,
                    attempts=attempts,
                    sleep=sleep,
                )
                stats["api_calls"] += 1
                rows = payload.get("rows") or []
                db.upsert_gsc_query_rows(
                    property_url, _api_rows_to_db_rows(property_url, day, list(dimensions), rows)
                )
                stats["rows_written"] += len(rows)
                total = payload.get("totalMatches")
                fetched = start_row + len(rows)
                if len(rows) < ROW_LIMIT or (total is not None and fetched >= int(total)):
                    break
                start_row += ROW_LIMIT
            db.mark_gsc_pull_complete(property_url, dim, day)
    db.update_gsc_property_synced(brand, utcnow())
    return stats


# ---------------------------------------------------------------------------
# inspect + sitemap
# ---------------------------------------------------------------------------


def inspect_url(
    db: Database,
    ws: Workspace,
    brand: str,
    url: str,
    *,
    api_base: str | None = None,
) -> dict[str, Any]:
    api_base = api_base or API_BASE
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError(f"invalid url '{url}': expected https://…")
    url = parsed.geturl()
    creds, prop = load_credentials(db, ws, brand)
    property_url = _require_property(prop, brand)
    token = refresh_access_token(creds)
    payload = (
        _api_request(
            "POST",
            f"{api_base}/v1/urlInspection/index:inspect",
            access_token=token,
            body={"inspectionUrl": url, "siteUrl": property_url},
            quota_project_id=creds.quota_project_id,
        )
        or {}
    )
    result = payload.get("inspectionResult") or {}
    index = result.get("indexStatusResult") or {}
    coverage = index.get("coverageState")
    mobile = result.get("mobileUsabilityResult") or {}
    row = {
        "property_url": property_url,
        "url": url,
        "inspected_at": utcnow(),
        "index_status": coverage,
        "mobile_usable": 1 if mobile.get("verdict") == "MOBILE_USABLE" else 0,
        "last_crawl": index.get("lastCrawlTime"),
    }
    db.upsert_gsc_inspection(row)
    return {
        "brand": brand,
        "property": property_url,
        "url": url,
        "index_status": COVERAGE_LABELS.get(coverage, coverage or "unspecified"),
        "coverage_state": coverage,
        "mobile_usable": bool(row["mobile_usable"]),
        "last_crawl": row["last_crawl"],
    }


def submit_sitemap(
    db: Database,
    ws: Workspace,
    brand: str,
    sitemap_url: str,
    *,
    api_base: str | None = None,
) -> dict[str, Any]:
    api_base = api_base or API_BASE
    parsed = urllib.parse.urlparse(sitemap_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError(f"invalid sitemap url '{sitemap_url}': expected https://…")
    sitemap_url = parsed.geturl()
    creds, prop = load_credentials(db, ws, brand)
    property_url = _require_property(prop, brand)
    token = refresh_access_token(creds)
    url = (
        f"{api_base}/webmasters/v3/sites/{urllib.parse.quote(property_url, safe='')}"
        f"/sitemaps/{urllib.parse.quote(sitemap_url, safe='')}"
    )
    _api_request("PUT", url, access_token=token, quota_project_id=creds.quota_project_id)
    return {
        "brand": brand,
        "property": property_url,
        "sitemap": sitemap_url,
        "submitted": True,
    }


# ---------------------------------------------------------------------------
# CSV import (path C fallback)
# ---------------------------------------------------------------------------


def _csv_number(value: str) -> float:
    cleaned = value.strip().replace(",", "").replace(" ", "")
    if not cleaned or cleaned in ("-", "—"):
        return 0.0
    if cleaned.endswith("%"):
        return float(cleaned[:-1]) / 100.0
    return float(cleaned)


def import_gsc_csv(
    db: Database,
    ws: Workspace,
    brand: str,
    csv_path: str | Path,
    *,
    property_url: str | None = None,
    data_date: str | None = None,
) -> dict[str, Any]:
    """Import a GSC UI export CSV (BOM/quotes tolerant) into gsc_queries.

    The CSV fallback needs no credentials — only a property (connected or
    --property). Rows land in the same table as `pull`, so insights work on
    imported data too.
    """
    prop = db.get_gsc_property(brand)
    resolved_property = property_url or (prop or {}).get("property_url")
    if not resolved_property:
        raise UsageError(
            f"brand '{brand}' has no GSC property; run `gsc connect --brand {brand} --property <url>` "
            "or pass --property <url>"
        )
    resolved_property = _validate_property(resolved_property)
    path = Path(csv_path)
    if not path.exists():
        raise UsageError(f"CSV file not found: {path}")

    def pick(header_map: dict[str, int], *names: str) -> str | None:
        return next((h for h in names if h in header_map), None)

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        header_map = {h: i for i, h in enumerate(headers)}
        query_col = pick(header_map, "Query", "Top queries")
        page_col = pick(header_map, "Page", "Top pages")
        date_col = pick(header_map, "Date")
        if query_col is None and page_col is None:
            raise UsageError(
                f"{path} does not look like a GSC export (no Query/Page column; headers: {headers})"
            )
        if date_col is None and not data_date:
            raise UsageError(
                f"{path} has no Date column; pass --date YYYY-MM-DD to set the data date"
            )
        if data_date:
            try:
                date.fromisoformat(data_date)
            except ValueError as exc:
                raise UsageError(f"invalid --date '{data_date}': expected YYYY-MM-DD") from exc

        rows: list[dict[str, Any]] = []
        skipped = 0
        for line_no, record in enumerate(reader, start=2):
            raw_query = (record.get(query_col) or "").strip() if query_col else ""
            raw_page = (record.get(page_col) or "").strip() if page_col else ""
            if not raw_query and not raw_page:
                skipped += 1
                continue
            day = data_date
            if not day:
                day = (record.get(date_col) or "").strip()
                try:
                    date.fromisoformat(day)
                except ValueError as exc:
                    raise UsageError(f"line {line_no}: invalid date {day!r}: expected YYYY-MM-DD") from exc
            ctr_raw = record.get("CTR") or ""
            rows.append(
                {
                    "property_url": resolved_property,
                    "data_date": day,
                    "query": raw_query,
                    "page": raw_page,
                    "device": None,
                    "country": None,
                    "search_type": "web",
                    "clicks": int(_csv_number(record.get("Clicks") or "0")),
                    "impressions": int(_csv_number(record.get("Impressions") or "0")),
                    "ctr": _csv_number(ctr_raw),
                    "position": _csv_number(record.get("Position") or "0"),
                    "pulled_at": utcnow(),
                }
            )
    written = db.upsert_gsc_query_rows(resolved_property, rows)
    return {
        "brand": brand,
        "property": resolved_property,
        "file": str(path),
        "rows_imported": written,
        "rows_skipped": skipped,
        "data_date": data_date or "from file",
        "columns": headers,
    }


# ---------------------------------------------------------------------------
# insights + status
# ---------------------------------------------------------------------------


def _audit_baseline(ws: Workspace, brand: str) -> dict[str, Any] | None:
    path = ws.root / "brands" / brand / "site-crawl" / "seo-audit.yaml"
    if not path.exists():
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(payload, dict) or payload.get("score") is None:
        return None
    return {"score": payload.get("score"), "rubric": payload.get("rubric")}


def insights(
    db: Database,
    ws: Workspace,
    brand: str,
    *,
    window: int = 28,
    min_impressions: int = 1000,
    max_ctr: float = 0.005,
    growth: float = 1.5,
    url: str | None = None,
) -> dict[str, Any]:
    """Three closed-loop reports over gsc_queries; suggestions only, no auto-action."""
    prop = db.get_gsc_property(brand)
    if prop is None or not prop.get("property_url"):
        raise GscError(
            f"brand '{brand}' has no connected GSC property; "
            f"run `seo-writer gsc connect --brand {brand} --property <url>`"
        )
    if window < 2:
        raise UsageError("--window must be at least 2 days")
    if growth <= 1:
        raise UsageError("--growth must be greater than 1")
    property_url = str(prop["property_url"])
    end = date.today() - timedelta(days=FRESHNESS_DELAY_DAYS)
    start = end - timedelta(days=window - 1)
    rows = db.gsc_query_rows(property_url, start.isoformat(), end.isoformat())

    by_query: dict[str, dict[str, float]] = {}
    for r in rows:
        q = r["query"]
        if not q:
            continue
        agg = by_query.setdefault(q, {"impressions": 0.0, "clicks": 0.0, "position_weighted": 0.0})
        impressions = float(r["impressions"] or 0)
        agg["impressions"] += impressions
        agg["clicks"] += float(r["clicks"] or 0)
        agg["position_weighted"] += float(r["position"] or 0) * impressions

    high_low: list[dict[str, Any]] = []
    for q, agg in by_query.items():
        impressions = int(agg["impressions"])
        clicks = int(agg["clicks"])
        if impressions < min_impressions:
            continue
        ctr = clicks / impressions if impressions else 0.0
        if ctr >= max_ctr:
            continue
        high_low.append(
            {
                "query": q,
                "impressions": impressions,
                "clicks": clicks,
                "ctr": round(ctr, 4),
                "avg_position": round(agg["position_weighted"] / impressions, 2) if impressions else None,
            }
        )
    high_low.sort(key=lambda item: item["impressions"], reverse=True)

    half_start = start + timedelta(days=(window - 1) // 2)
    half_key = half_start.isoformat()
    first: dict[str, dict[str, int]] = {}
    second: dict[str, dict[str, int]] = {}
    for r in rows:
        q = r["query"]
        if not q:
            continue
        bucket = first if r["data_date"] < half_key else second
        agg = bucket.setdefault(q, {"impressions": 0, "clicks": 0})
        agg["impressions"] += int(r["impressions"] or 0)
        agg["clicks"] += int(r["clicks"] or 0)

    rising: list[dict[str, Any]] = []
    for q, agg1 in first.items():
        agg2 = second.get(q)
        if agg2 is None:
            continue
        imp1 = agg1["impressions"]
        imp2 = agg2["impressions"]
        if imp1 < 20 or imp2 < imp1 * growth:
            continue
        rising.append(
            {
                "query": q,
                "impressions_first_half": imp1,
                "impressions_second_half": imp2,
                "growth": round(imp2 / imp1, 2),
            }
        )
    rising.sort(key=lambda item: item["growth"], reverse=True)

    return {
        "brand": brand,
        "property": property_url,
        "window_days": window,
        "range": [start.isoformat(), end.isoformat()],
        "high_impressions_low_ctr": high_low[:20],
        "rising_queries": rising[:20],
        "url_performance": _url_performance(ws, brand, rows, url) if url else None,
    }


def _url_performance(
    ws: Workspace, brand: str, rows: list[dict[str, Any]], url: str
) -> dict[str, Any]:
    page_rows = [r for r in rows if (r["page"] or "") == url]
    if not page_rows:
        return {
            "url": url,
            "has_data": False,
            "clicks": 0,
            "impressions": 0,
            "ctr": 0.0,
            "avg_position": None,
        }

    def avg_position(items: list[dict[str, Any]]) -> float | None:
        impressions = sum(float(r["impressions"] or 0) for r in items)
        if not impressions:
            return None
        weighted = sum(float(r["position"] or 0) * float(r["impressions"] or 0) for r in items)
        return round(weighted / impressions, 2)

    impressions = sum(float(r["impressions"] or 0) for r in page_rows)
    clicks = sum(float(r["clicks"] or 0) for r in page_rows)
    dates = sorted({r["data_date"] for r in page_rows})
    mid = dates[len(dates) // 2]
    first = [r for r in page_rows if r["data_date"] < mid]
    second = [r for r in page_rows if r["data_date"] >= mid]
    return {
        "url": url,
        "has_data": True,
        "clicks": int(clicks),
        "impressions": int(impressions),
        "ctr": round(clicks / impressions, 4) if impressions else 0.0,
        "avg_position": avg_position(page_rows),
        "position_first_half": avg_position(first),
        "position_second_half": avg_position(second),
        "audit_baseline": _audit_baseline(ws, brand),
    }


def gsc_status(db: Database, ws: Workspace, brand: str) -> dict[str, Any]:
    """Brand ↔ property binding, sync range and credential path state (no secrets)."""
    prop = db.get_gsc_property(brand)
    client_file = client_json_path(ws, brand)
    result: dict[str, Any] = {
        "brand": brand,
        "connected": prop is not None,
        "credentials": {
            "adc_file": str(GCLOUD_ADC_PATH) if GCLOUD_ADC_PATH.exists() else None,
            "adc_file_exists": GCLOUD_ADC_PATH.exists(),
            "own_client_json_path": str(client_file) if client_file.exists() else None,
            "own_client_json_exists": client_file.exists(),
            "token_file_exists": token_file_path(ws, brand).exists(),
        },
    }
    if prop is None:
        return result
    result["property"] = prop["property_url"]
    result["auth_path"] = prop["auth_path"]
    result["client_json_path"] = prop.get("client_json_path")
    result["last_synced_at"] = prop.get("last_synced_at")
    result["sync"] = db.gsc_sync_range(prop["property_url"])
    return result


# ---------------------------------------------------------------------------
# setup / auth
# ---------------------------------------------------------------------------


def setup_guide(
    db: Database,
    ws: Workspace,
    brand: str,
    *,
    client_json: str | None = None,
    interactive: bool = False,
    adc_path: Path | None = None,
    prompt: Callable[[str], str] | None = None,
    token_url: str | None = None,
    tokeninfo_url: str | None = None,
) -> dict[str, Any]:
    """Detect the auth path and guide setup; gcloud ADC (path A) is preferred."""
    if client_json:
        result = import_client_json(ws, brand, client_json)
        result["status"] = "client-imported"
        return result
    adc = adc_path or GCLOUD_ADC_PATH
    if adc.exists():
        try:
            creds = load_adc(adc)
            verification = verify_credentials(creds, token_url=token_url, tokeninfo_url=tokeninfo_url)
        except GscAuthError as exc:
            return {
                "status": "adc-invalid",
                "adc_file": str(adc),
                "message": str(exc),
                "next": f're-run: gcloud auth application-default login --scopes="{SCOPE}"',
            }
        if not verification["has_webmasters"]:
            return {
                "status": "adc-missing-scope",
                "adc_file": str(adc),
                "message": verification["message"],
                "next": f're-run: gcloud auth application-default login --scopes="{SCOPE}"',
            }
        return {
            "status": "ready",
            "adc_file": str(adc),
            "auth_path": "gcloud-adc",
            "verification": verification,
            "next": (
                f"run `seo-writer gsc auth --brand {brand}` for the one-time browser consent, "
                f"or go straight to `seo-writer gsc sites --brand {brand}`"
            ),
        }
    if interactive:
        if prompt is None:
            raise GscError("interactive setup needs a terminal")
        answer = prompt(
            "No gcloud ADC found. Paste the path of a Desktop-app client json (path B), "
            "or press Enter for the gcloud instructions"
        )
        if answer.strip():
            result = import_client_json(ws, brand, answer)
            result["status"] = "client-imported"
            return result
    return {
        "status": "no-credentials",
        "options": [
            {
                "path": "A",
                "title": "gcloud ADC (preferred — the customer never touches GCP)",
                "steps": [
                    "install the Google Cloud SDK once (we do this during delivery)",
                    f'gcloud auth application-default login --scopes="{SCOPE}"',
                    f"then run `seo-writer gsc auth --brand {brand}`",
                ],
            },
            {
                "path": "B",
                "title": "self-built OAuth client (when the SDK cannot be installed)",
                "steps": [
                    "4 deep links: create a project → enable the Search Console API → consent screen → "
                    "download the Desktop-app client json",
                    f"then run `seo-writer gsc setup --brand {brand} --client-json <path>`",
                    f"then run `seo-writer gsc auth --brand {brand}`",
                ],
            },
        ],
        "next": f"path A is preferred; continue with `seo-writer gsc auth --brand {brand}`",
    }


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str, code_challenge: str, *, scope: str = SCOPE) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    *,
    token_url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, str]:
    token_url = token_url or TOKEN_URL
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    status, body = _http_request(
        "POST",
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=data,
        timeout=timeout,
    )
    if status != 200:
        _raise_for_status(status, body, "authorization code exchange")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GscError("token endpoint returned a non-JSON response") from exc
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise GscAuthError(
            "authorization response carried no refresh_token; "
            "the auth URL must use access_type=offline&prompt=consent"
        )
    return {
        "access_token": str(payload.get("access_token") or ""),
        "refresh_token": refresh,
        "scope": str(payload.get("scope") or ""),
    }


class _CodeReceiver(BaseHTTPRequestHandler):
    """Single-shot loopback receiver for the OAuth redirect (desktop flow)."""

    def do_GET(self) -> None:  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in query:
            self._finish(200, "Authorization successful — you can close this window.", query["code"][0])
        elif "error" in query:
            self._finish(400, f"Authorization failed: {query['error'][0]}", "error:" + query["error"][0])
        else:
            self._finish(400, "Missing code in the redirect.", "")

    def _finish(self, status: int, text: str, code: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if code:
            self.server.receiver_result.append(code)

    def log_message(self, *args: Any) -> None:  # silence test noise
        pass


class _LoopbackReceiver:
    """One-shot loopback server; start it BEFORE opening the browser so a
    fast click never races the listener."""

    def __init__(self, port: int, timeout: float = AUTH_TIMEOUT_S) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), _CodeReceiver)
        self.server.receiver_result: list[str] = []
        self._timeout = timeout
        self._deadline = time.monotonic() + timeout
        threading.Thread(target=self.server.handle_request, daemon=True).start()

    def wait_code(self) -> str:
        while time.monotonic() < self._deadline:
            if self.server.receiver_result:
                result = self.server.receiver_result[0]
                if result.startswith("error:"):
                    raise GscError(result)
                return result
            time.sleep(0.2)
        raise GscError(
            f"authorization timed out after {self._timeout:.0f}s; re-run `gsc auth`"
        )

    def close(self) -> None:
        self.server.server_close()


def receive_code_via_loopback(port: int, timeout: float = AUTH_TIMEOUT_S) -> str:
    """Serve one redirect on 127.0.0.1:<port> and return the authorization code."""
    receiver = _LoopbackReceiver(port, timeout)
    try:
        return receiver.wait_code()
    finally:
        receiver.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_browser(url: str) -> None:
    webbrowser.open(url)


def desktop_auth(
    ws: Workspace,
    brand: str,
    *,
    no_launch_browser: bool = False,
    timeout: float = AUTH_TIMEOUT_S,
    launch: Callable[[str], None] = _open_browser,
    prompt: Callable[[str], str] | None = None,
    token_url: str | None = None,
) -> dict[str, Any]:
    """PKCE desktop flow for the own-client path (loopback or pasted-code)."""
    creds = load_client_json(ws, brand)
    verifier, challenge = pkce_pair()
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"
    auth_url = build_auth_url(creds.client_id, redirect_uri, challenge)
    if no_launch_browser:
        if prompt is None:
            raise GscError("--no-launch-browser needs an interactive terminal to paste the code")
        code = prompt(
            "Open this URL, authorize, then paste the code from the redirect back:\n"
            f"{auth_url}\ncode> "
        )
    else:
        receiver = _LoopbackReceiver(port, timeout)
        try:
            launch(auth_url)
            code = receiver.wait_code()
        finally:
            receiver.close()
    tokens = exchange_code(
        creds.client_id,
        creds.client_secret,
        code.strip(),
        verifier,
        redirect_uri,
        token_url=token_url,
    )
    save_token(ws, brand, tokens["refresh_token"])
    return {
        "brand": brand,
        "auth_path": "own-client",
        "status": "authorized",
        "client_json": creds.client_json_path,
        "token_file": str(token_file_path(ws, brand)),
        "scopes": tokens["scope"],
        "next": f"run `seo-writer gsc sites --brand {brand}` to see your properties",
    }


def gcloud_auth(
    *,
    no_launch_browser: bool = False,
    gcloud_bin: str = GCLOUD_BIN,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Authorize via gcloud application-default login (customer does this once)."""
    cmd = [gcloud_bin, "auth", "application-default", "login", "--scopes", SCOPE]
    if no_launch_browser:
        cmd.append("--no-launch-browser")
    proc = run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise GscError(f"gcloud authorization failed ({detail}); run it manually: {' '.join(cmd)}")
    return {
        "auth_path": "gcloud-adc",
        "status": "authorized",
        "command": " ".join(cmd),
        "next": "credentials live in ~/.config/gcloud/application_default_credentials.json; "
        "run `seo-writer gsc sites --brand <brand>` next",
    }


def run_auth(
    db: Database,
    ws: Workspace,
    brand: str,
    *,
    no_launch_browser: bool = False,
    prompt: Callable[[str], str] | None = None,
    timeout: float = AUTH_TIMEOUT_S,
    launch: Callable[[str], None] = _open_browser,
    gcloud_bin: str = GCLOUD_BIN,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Route auth: own-client PKCE when configured/connected, else gcloud ADC."""
    prop = db.get_gsc_property(brand)
    auth_path = (prop or {}).get("auth_path")
    use_own_client = auth_path == "own-client" or (
        auth_path is None and client_json_path(ws, brand).exists() and not GCLOUD_ADC_PATH.exists()
    )
    if use_own_client:
        if not client_json_path(ws, brand).exists():
            raise GscError(
                f"brand '{brand}' has no own client json; "
                f"run `seo-writer gsc setup --brand {brand} --client-json <path>` first"
            )
        return desktop_auth(
            ws, brand, no_launch_browser=no_launch_browser, prompt=prompt, timeout=timeout, launch=launch
        )
    return gcloud_auth(no_launch_browser=no_launch_browser, gcloud_bin=gcloud_bin, run=run)
