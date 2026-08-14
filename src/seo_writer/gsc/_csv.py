"""CSV import (path C fallback): GSC UI exports → daily insights without credentials."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Workspace
from ..db import Database
from ..errors import UsageError
from ..ids import utcnow
from ._http import _validate_property


def _csv_number(value: str, *, line_no: int, column: str) -> float:
    # strip thousands separators: ASCII commas plus U+00A0 (non-breaking
    # space, used by Excel-style exports as a grouping separator)
    cleaned = value.strip().replace(",", "").replace("\xa0", "")
    if not cleaned or cleaned in ("-", "—"):
        return 0.0
    divisor = 100.0 if cleaned.endswith("%") else 1.0
    if divisor != 1.0:
        cleaned = cleaned[:-1]
    try:
        return float(cleaned) / divisor
    except ValueError as exc:
        raise UsageError(
            f"line {line_no}: invalid {column} {value.strip()!r}: expected a number"
        ) from exc


def import_gsc_csv(
    db: Database,
    ws: Workspace,
    brand: str,
    csv_path: str | Path,
    *,
    property_url: str | None = None,
) -> dict[str, Any]:
    """Import a GSC UI export CSV (BOM/quotes tolerant) into gsc_queries.

    The CSV fallback needs no credentials — only a property (connected or
    --property). Rows land in the same table as `pull`, so insights work on
    imported data too.
    """
    prop = db.get_gsc_property(brand)
    requested_property = _validate_property(property_url) if property_url else None
    bound_property = (prop or {}).get("property_url")
    if requested_property and bound_property and requested_property != bound_property:
        raise UsageError(
            f"brand '{brand}' is already bound to {bound_property}; refusing to mix properties"
        )
    resolved_property = requested_property or bound_property
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
        if date_col is None:
            raise UsageError(f"{path} has no Date column; range-aggregate CSV cannot enter daily insights")

        rows: list[dict[str, Any]] = []
        skipped = 0
        for line_no, record in enumerate(reader, start=2):
            raw_query = (record.get(query_col) or "").strip() if query_col else ""
            raw_page = (record.get(page_col) or "").strip() if page_col else ""
            if not raw_query and not raw_page:
                skipped += 1
                continue
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
                    "clicks": int(_csv_number(record.get("Clicks") or "0", line_no=line_no, column="Clicks")),
                    "impressions": int(
                        _csv_number(record.get("Impressions") or "0", line_no=line_no, column="Impressions")
                    ),
                    "ctr": _csv_number(ctr_raw, line_no=line_no, column="CTR"),
                    "position": _csv_number(
                        record.get("Position") or "0", line_no=line_no, column="Position"
                    ),
                    "pulled_at": utcnow(),
                }
            )
    written = db.upsert_gsc_query_rows(resolved_property, rows)
    if prop is None or prop.get("auth_path") == "csv-import":
        db.upsert_gsc_property(brand, resolved_property, "csv-import", None, status="imported")
    return {
        "brand": brand,
        "property": resolved_property,
        "file": str(path),
        "rows_imported": written,
        "rows_skipped": skipped,
        "data_date": "from file",
        "source": "csv-import",
        "columns": headers,
    }
