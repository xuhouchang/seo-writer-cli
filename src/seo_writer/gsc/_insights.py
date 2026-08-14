"""Closed-loop insights and brand↔property status reporting."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import yaml

from .. import gsc as _gsc
from ..config import Workspace
from ..db import Database
from ..errors import UsageError
from ._auth import client_json_path, token_file_path
from ._constants import FRESHNESS_DELAY_DAYS
from ._errors import GscError


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

    # Data completeness: API data is only trusted per fully-paginated
    # (dimension, date) chunk recorded in gsc_pull_state; CSV imports are
    # trusted per date present in the file. "partial" reflects actual window
    # coverage instead of being hardcoded.
    if prop.get("auth_path") == "csv-import":
        expected = window
        covered = len({r["data_date"] for r in rows})
    else:
        expected = window * 2  # (date,query) + (date,page) chunks per day
        covered = db.gsc_pull_completed_in_range(property_url, start.isoformat(), end.isoformat())
    coverage = covered / expected if expected else 0.0

    return {
        "brand": brand,
        "property": property_url,
        "window_days": window,
        "range": [start.isoformat(), end.isoformat()],
        "source": "csv-import" if prop.get("auth_path") == "csv-import" else "api",
        "partial": coverage < 1.0,
        "coverage": round(coverage, 3),
        "data_limit_note": (
            "Search Console returns the most important rows subject to internal limits; "
            "this is not a complete keyword database."
        ),
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
        "connected": prop is not None and prop.get("status") == "connected",
        "credentials": {
            "adc_file": str(_gsc.GCLOUD_ADC_PATH) if _gsc.GCLOUD_ADC_PATH.exists() else None,
            "adc_file_exists": _gsc.GCLOUD_ADC_PATH.exists(),
            "own_client_json_path": str(client_file) if client_file.exists() else None,
            "own_client_json_exists": client_file.exists(),
            "token_file_exists": token_file_path(ws, brand).exists(),
        },
    }
    if prop is None:
        return result
    result["property"] = prop["property_url"]
    result["auth_path"] = prop["auth_path"]
    result["source"] = "csv-import" if prop["auth_path"] == "csv-import" else "api"
    result["status"] = prop["status"]
    result["client_json_path"] = prop.get("client_json_path")
    result["last_synced_at"] = prop.get("last_synced_at")
    result["sync"] = db.gsc_sync_range(prop["property_url"])
    return result
