"""Sites listing, property connect, search-analytics pull and URL inspection."""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from .. import gsc as _gsc
from ..config import Workspace
from ..db import Database
from ..errors import UsageError
from ..ids import utcnow
from ._auth import Credentials
from ._backoff import RateLimiter, with_backoff
from ._constants import (
    COVERAGE_LABELS,
    DEFAULT_PULL_DAYS,
    FRESHNESS_DELAY_DAYS,
    MAX_AUTH_REFRESH_RETRIES,
    MAX_PULL_DAYS,
)
from ._errors import GscAuthError, GscError
from ._http import _api_request, _require_property, _validate_property, refresh_access_token


def list_sites(creds: Credentials, *, api_base: str | None = None) -> dict[str, Any]:
    api_base = api_base or _gsc.API_BASE
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
    creds, _ = _gsc.load_credentials(db, ws, brand)
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
        "rowLimit": _gsc.ROW_LIMIT,
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
    api_base = api_base or _gsc.API_BASE
    creds, prop = _gsc.load_credentials(db, ws, brand)
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
        "data_limit_note": (
            "Search Console returns the most important rows subject to internal limits; "
            "this is not a complete keyword database."
        ),
    }
    token: str | None = None
    auth_failures = 0

    def counted_query(*args: Any, **kwargs: Any) -> dict[str, Any]:
        stats["api_calls"] += 1
        return _query_page(*args, **kwargs)

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
                try:
                    payload = with_backoff(
                        counted_query,
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
                except GscAuthError:
                    # An access token lives ~1h; a long-window pull can outlive
                    # it mid-run, so refresh and retry the same page instead of
                    # failing the whole pull. Re-pulling is idempotent: rows are
                    # upserted and the day is only marked complete once every
                    # page succeeds. Bounded: a refresh that still gets 401s
                    # (revoked access) propagates after a few attempts.
                    auth_failures += 1
                    if auth_failures > MAX_AUTH_REFRESH_RETRIES:
                        raise
                    token = refresh_access_token(creds)
                    continue
                auth_failures = 0
                rows = payload.get("rows") or []
                db.upsert_gsc_query_rows(
                    property_url, _api_rows_to_db_rows(property_url, day, list(dimensions), rows)
                )
                stats["rows_written"] += len(rows)
                total = payload.get("totalMatches")
                fetched = start_row + len(rows)
                if len(rows) < _gsc.ROW_LIMIT or (total is not None and fetched >= int(total)):
                    break
                start_row += _gsc.ROW_LIMIT
            db.mark_gsc_pull_complete(property_url, dim, day)
    db.update_gsc_property_synced(brand, utcnow())
    return stats


def inspect_url(
    db: Database,
    ws: Workspace,
    brand: str,
    url: str,
    *,
    api_base: str | None = None,
) -> dict[str, Any]:
    api_base = api_base or _gsc.API_BASE
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError(f"invalid url '{url}': expected https://…")
    url = parsed.geturl()
    creds, prop = _gsc.load_credentials(db, ws, brand)
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
        "mobile_usable": (
            1 if mobile.get("verdict") == "MOBILE_USABLE"
            else 0 if mobile.get("verdict") else None
        ),
        "last_crawl": index.get("lastCrawlTime"),
    }
    db.upsert_gsc_inspection(row)
    return {
        "brand": brand,
        "property": property_url,
        "url": url,
        "index_status": COVERAGE_LABELS.get(coverage, coverage or "unspecified"),
        "coverage_state": coverage,
        "mobile_usable": None if row["mobile_usable"] is None else bool(row["mobile_usable"]),
        "last_crawl": row["last_crawl"],
    }
