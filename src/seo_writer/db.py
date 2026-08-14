"""SQLite persistence: relational state for the workspace.

Object artifacts (markdown/json/exports) live in the workspace objects dir;
SQLite stores references and hashes. All writes go through this module so the
schema stays in one place.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .ids import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS brands (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  brand_id INTEGER NOT NULL REFERENCES brands(id),
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(brand_id, slug)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  status TEXT NOT NULL,
  step TEXT,
  brief_snapshot TEXT NOT NULL,
  facts_hash TEXT NOT NULL,
  facts_version INTEGER NOT NULL,
  outline_revision INTEGER NOT NULL DEFAULT 0,
  approved_revision INTEGER,
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  evidence_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  fetch_method TEXT NOT NULL,
  opened_current_run INTEGER NOT NULL DEFAULT 0,
  evidence_origin TEXT NOT NULL,
  platform TEXT,
  url TEXT,
  title TEXT,
  grade TEXT,
  summary TEXT,
  details TEXT NOT NULL DEFAULT '{}',
  UNIQUE(run_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);

CREATE TABLE IF NOT EXISTS outlines (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, revision)
);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  outline_revision INTEGER NOT NULL,
  approver TEXT NOT NULL,
  facts_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  superseded_at TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  outline_revision INTEGER NOT NULL,
  article TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY,
  run_id TEXT,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);

CREATE TABLE IF NOT EXISTS cost_ledger (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_profile TEXT,
  operation TEXT NOT NULL,
  cost_estimate REAL NOT NULL DEFAULT 0,
  token_estimate INTEGER,
  request_fingerprint TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(idempotency_key, provider, operation)
);

CREATE TABLE IF NOT EXISTS command_ledger (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  command TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, command, idempotency_key)
);

CREATE TABLE IF NOT EXISTS fact_rules (
  id INTEGER PRIMARY KEY,
  brand_id INTEGER NOT NULL REFERENCES brands(id),
  claim_id TEXT NOT NULL,
  claim TEXT NOT NULL,
  safety_level TEXT NOT NULL,
  allowed_wording TEXT,
  disallowed_wording TEXT NOT NULL DEFAULT '[]',
  source_url TEXT,
  evidence_date TEXT,
  evidence_mode TEXT,
  volatility TEXT,
  guardrail TEXT,
  reason TEXT,
  decision TEXT NOT NULL,
  facts_version INTEGER NOT NULL,
  UNIQUE(brand_id, claim_id)
);

CREATE TABLE IF NOT EXISTS fact_snapshots (
  id INTEGER PRIMARY KEY,
  brand_id INTEGER NOT NULL,
  snapshot_hash TEXT NOT NULL,
  facts_version INTEGER NOT NULL,
  content TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brand_policies (
  brand_id INTEGER PRIMARY KEY REFERENCES brands(id),
  policy_json TEXT NOT NULL,
  policy_hash TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gsc_properties (
  brand TEXT NOT NULL,
  property_url TEXT NOT NULL,
  auth_path TEXT NOT NULL,
  client_json_path TEXT,
  status TEXT NOT NULL,
  last_synced_at TEXT,
  PRIMARY KEY (brand)
);

CREATE TABLE IF NOT EXISTS gsc_queries (
  property_url TEXT NOT NULL,
  data_date TEXT NOT NULL,
  query TEXT NOT NULL,
  page TEXT NOT NULL DEFAULT '',
  device TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  search_type TEXT NOT NULL DEFAULT 'web',
  clicks INTEGER NOT NULL DEFAULT 0,
  impressions INTEGER NOT NULL DEFAULT 0,
  ctr REAL NOT NULL DEFAULT 0,
  position REAL NOT NULL DEFAULT 0,
  pulled_at TEXT NOT NULL,
  PRIMARY KEY (property_url, data_date, query, page, device, country, search_type)
);
CREATE INDEX IF NOT EXISTS idx_gsc_queries_prop_date ON gsc_queries(property_url, data_date);

CREATE TABLE IF NOT EXISTS gsc_inspections (
  property_url TEXT NOT NULL,
  url TEXT NOT NULL,
  inspected_at TEXT NOT NULL,
  index_status TEXT,
  mobile_usable INTEGER,
  last_crawl TEXT,
  PRIMARY KEY (property_url, url)
);

CREATE TABLE IF NOT EXISTS gsc_pull_state (
  property_url TEXT NOT NULL,
  dimension TEXT NOT NULL,
  data_date TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  PRIMARY KEY (property_url, dimension, data_date)
);
"""


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate_gsc_queries()

    def _migrate_gsc_queries(self) -> None:
        """Rebuild the legacy nullable-key table and deterministically dedupe it."""
        columns = self._conn.execute("PRAGMA table_info(gsc_queries)").fetchall()
        nullable = {row[1] for row in columns if row[1] in {"page", "device", "country"} and not row[3]}
        if not nullable:
            return
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DROP INDEX IF EXISTS idx_gsc_queries_prop_date")
            self._conn.execute("ALTER TABLE gsc_queries RENAME TO gsc_queries_legacy")
            self._conn.execute(
                """CREATE TABLE gsc_queries (
                  property_url TEXT NOT NULL, data_date TEXT NOT NULL, query TEXT NOT NULL,
                  page TEXT NOT NULL DEFAULT '', device TEXT NOT NULL DEFAULT '',
                  country TEXT NOT NULL DEFAULT '',
                  search_type TEXT NOT NULL DEFAULT 'web', clicks INTEGER NOT NULL DEFAULT 0,
                  impressions INTEGER NOT NULL DEFAULT 0, ctr REAL NOT NULL DEFAULT 0,
                  position REAL NOT NULL DEFAULT 0, pulled_at TEXT NOT NULL,
                  PRIMARY KEY (property_url, data_date, query, page, device, country, search_type)
                )"""
            )
            self._conn.execute(
                """INSERT INTO gsc_queries
                SELECT property_url, data_date, query, COALESCE(page, ''), COALESCE(device, ''),
                       COALESCE(country, ''), search_type, clicks, impressions, ctr, position, pulled_at
                FROM (
                  SELECT legacy.*, ROW_NUMBER() OVER (
                    PARTITION BY property_url, data_date, query, COALESCE(page, ''),
                                 COALESCE(device, ''), COALESCE(country, ''), search_type
                    ORDER BY pulled_at DESC, rowid DESC
                  ) AS rn
                  FROM gsc_queries_legacy AS legacy
                ) WHERE rn = 1"""
            )
            self._conn.execute(
                "CREATE INDEX idx_gsc_queries_prop_date ON gsc_queries(property_url, data_date)"
            )
            self._conn.execute("DROP TABLE gsc_queries_legacy")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ---- brands ----

    def create_brand(self, slug: str, name: str) -> dict[str, Any]:
        now = utcnow()
        cur = self._conn.execute(
            "INSERT INTO brands (slug, name, created_at) VALUES (?, ?, ?)", (slug, name, now)
        )
        self._conn.commit()
        return {"id": cur.lastrowid, "slug": slug, "name": name, "created_at": now}

    def get_brand(self, slug: str) -> dict[str, Any] | None:
        return _row_to_dict(self._conn.execute("SELECT * FROM brands WHERE slug = ?", (slug,)).fetchone())

    def list_brands(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM brands ORDER BY slug").fetchall()]

    # ---- projects ----

    def create_project(self, brand_id: int, slug: str, title: str) -> dict[str, Any]:
        now = utcnow()
        cur = self._conn.execute(
            "INSERT INTO projects (brand_id, slug, title, created_at) VALUES (?, ?, ?, ?)",
            (brand_id, slug, title, now),
        )
        self._conn.commit()
        return {"id": cur.lastrowid, "brand_id": brand_id, "slug": slug, "title": title}

    def get_project(self, brand_id: int, slug: str) -> dict[str, Any] | None:
        return _row_to_dict(
            self._conn.execute(
                "SELECT * FROM projects WHERE brand_id = ? AND slug = ?", (brand_id, slug)
            ).fetchone()
        )

    def list_projects(self, brand_id: int | None = None) -> list[dict[str, Any]]:
        if brand_id is None:
            return [dict(r) for r in self._conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM projects WHERE brand_id = ? ORDER BY id", (brand_id,)
            ).fetchall()
        ]

    # ---- runs ----

    def create_run(
        self,
        run_id: str,
        project_id: int,
        brief_snapshot: dict[str, Any],
        facts_hash: str,
        facts_version: int,
    ) -> dict[str, Any]:
        now = utcnow()
        self._conn.execute(
            "INSERT INTO runs (id, project_id, status, brief_snapshot, facts_hash, facts_version,"
            " created_at, updated_at) VALUES (?, ?, 'created', ?, ?, ?, ?, ?)",
            (
                run_id,
                project_id,
                json.dumps(brief_snapshot, ensure_ascii=False),
                facts_hash,
                facts_version,
                now,
                now,
            ),
        )
        self._conn.commit()
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return _row_to_dict(self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def list_runs(self, project_id: int | None = None) -> list[dict[str, Any]]:
        if project_id is None:
            return [dict(r) for r in self._conn.execute("SELECT * FROM runs ORDER BY created_at").fetchall()]
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY created_at", (project_id,)
            ).fetchall()
        ]

    def set_status(
        self, run_id: str, status: str, step: str | None = None, failure_reason: str | None = None
    ) -> None:
        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, utcnow()]
        if step is not None:
            fields.append("step = ?")
            values.append(step)
        if failure_reason is not None:
            fields.append("failure_reason = ?")
            values.append(failure_reason)
        elif status not in {"blocked"}:
            fields.append("failure_reason = NULL")
        values.append(run_id)
        self._conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", values)
        self._conn.commit()

    def set_outline_revision(self, run_id: str, revision: int) -> None:
        self._conn.execute(
            "UPDATE runs SET outline_revision = ?, updated_at = ? WHERE id = ?", (revision, utcnow(), run_id)
        )
        self._conn.commit()

    def set_approved_revision(self, run_id: str, revision: int | None) -> None:
        self._conn.execute(
            "UPDATE runs SET approved_revision = ?, updated_at = ? WHERE id = ?", (revision, utcnow(), run_id)
        )
        self._conn.commit()

    # ---- evidence ----

    def replace_run_evidence(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        self._conn.execute("DELETE FROM evidence WHERE run_id = ?", (run_id,))
        for row in rows:
            self._conn.execute(
                "INSERT INTO evidence (run_id, evidence_id, source_type, fetch_method, opened_current_run,"
                " evidence_origin, platform, url, title, grade, summary, details)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    row["evidence_id"],
                    row["source_type"],
                    row["fetch_method"],
                    1 if row.get("opened_current_run") else 0,
                    row["evidence_origin"],
                    row.get("platform"),
                    row.get("url"),
                    row.get("title"),
                    row.get("grade"),
                    row.get("summary", ""),
                    json.dumps(row.get("details", {}), ensure_ascii=False),
                ),
            )
        self._conn.commit()

    def list_evidence(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM evidence WHERE run_id = ? ORDER BY evidence_id", (run_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"] or "{}")
            out.append(d)
        return out

    # ---- outlines ----

    def add_outline(self, run_id: str, revision: int, content: str) -> None:
        self._conn.execute(
            "INSERT INTO outlines (run_id, revision, content, created_at) VALUES (?, ?, ?, ?)",
            (run_id, revision, content, utcnow()),
        )
        self._conn.commit()

    def get_outline(self, run_id: str, revision: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self._conn.execute(
                "SELECT * FROM outlines WHERE run_id = ? AND revision = ?", (run_id, revision)
            ).fetchone()
        )

    def latest_outline(self, run_id: str) -> dict[str, Any] | None:
        return _row_to_dict(
            self._conn.execute(
                "SELECT * FROM outlines WHERE run_id = ? ORDER BY revision DESC LIMIT 1", (run_id,)
            ).fetchone()
        )

    def list_outlines(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM outlines WHERE run_id = ? ORDER BY revision", (run_id,)
            ).fetchall()
        ]

    # ---- approvals ----

    def add_approval(
        self, run_id: str, outline_revision: int, approver: str, facts_hash: str
    ) -> dict[str, Any]:
        cur = self._conn.execute(
            "INSERT INTO approvals (run_id, outline_revision, approver, facts_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, outline_revision, approver, facts_hash, utcnow()),
        )
        self._conn.commit()
        return {
            "id": cur.lastrowid,
            "run_id": run_id,
            "outline_revision": outline_revision,
            "approver": approver,
            "facts_hash": facts_hash,
        }

    def get_approval(self, run_id: str, outline_revision: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self._conn.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND outline_revision = ? ORDER BY id DESC LIMIT 1",
                (run_id, outline_revision),
            ).fetchone()
        )

    def list_approvals(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM approvals WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        ]

    def supersede_approvals(self, run_id: str, before_revision: int) -> None:
        now = utcnow()
        self._conn.execute(
            "UPDATE approvals SET superseded_at = ? WHERE run_id = ? AND outline_revision < ?"
            " AND superseded_at IS NULL",
            (now, run_id, before_revision),
        )
        self._conn.commit()

    # ---- drafts ----

    def save_draft(self, run_id: str, outline_revision: int, article: str, metadata: dict[str, Any]) -> None:
        self._conn.execute("DELETE FROM drafts WHERE run_id = ?", (run_id,))
        self._conn.execute(
            "INSERT INTO drafts (run_id, outline_revision, article, metadata, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, outline_revision, article, json.dumps(metadata, ensure_ascii=False), utcnow()),
        )
        self._conn.commit()

    def get_draft(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM drafts WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["metadata"] = json.loads(d["metadata"] or "{}")
        return d

    # ---- audit ----

    def add_audit(self, run_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO audit_events (run_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            (run_id, event_type, json.dumps(payload, ensure_ascii=False, default=str), utcnow()),
        )
        self._conn.commit()

    def list_audit(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            return [dict(r) for r in self._conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()]
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM audit_events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        ]

    # ---- costs ----

    def add_cost(self, row: dict[str, Any]) -> bool:
        """Record one provider cost; returns False when the idempotency key already exists."""
        try:
            self._conn.execute(
                "INSERT INTO cost_ledger (run_id, idempotency_key, provider, provider_profile, operation,"
                " cost_estimate, token_estimate, request_fingerprint, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["run_id"],
                    row["idempotency_key"],
                    row["provider"],
                    row.get("provider_profile"),
                    row["operation"],
                    row.get("cost_estimate", 0.0),
                    row.get("token_estimate"),
                    row.get("request_fingerprint"),
                    utcnow(),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False

    def list_costs(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM cost_ledger WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        ]

    def cost_total(self, run_id: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_estimate), 0) AS total FROM cost_ledger WHERE run_id = ?", (run_id,)
        ).fetchone()
        return float(row["total"])

    # ---- command ledger (idempotency) ----

    def record_command(self, run_id: str, command: str, idempotency_key: str, result: dict[str, Any]) -> bool:
        try:
            self._conn.execute(
                "INSERT INTO command_ledger (run_id, command, idempotency_key, result_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    command,
                    idempotency_key,
                    json.dumps(result, ensure_ascii=False, default=str),
                    utcnow(),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False

    def get_command_result(self, run_id: str, command: str, idempotency_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM command_ledger WHERE run_id = ? AND command = ? AND idempotency_key = ?",
            (run_id, command, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["result_json"])

    # ---- facts ----

    def replace_fact_rules(self, brand_id: int, rules: list[dict[str, Any]], facts_version: int) -> None:
        self._conn.execute("DELETE FROM fact_rules WHERE brand_id = ?", (brand_id,))
        for r in rules:
            self._conn.execute(
                "INSERT INTO fact_rules (brand_id, claim_id, claim, safety_level, allowed_wording,"
                " disallowed_wording, source_url, evidence_date, evidence_mode, volatility, guardrail,"
                " reason, decision, facts_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    brand_id,
                    r["claim_id"],
                    r["claim"],
                    r["safety_level"],
                    r.get("allowed_wording", ""),
                    json.dumps(r.get("disallowed_wording", []), ensure_ascii=False),
                    r.get("source_url", ""),
                    r.get("evidence_date", ""),
                    r.get("evidence_mode", ""),
                    r.get("volatility", "medium"),
                    r.get("guardrail", ""),
                    r.get("reason", ""),
                    r["decision"],
                    facts_version,
                ),
            )
        self._conn.commit()

    def get_fact_rules(self, brand_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM fact_rules WHERE brand_id = ? ORDER BY claim_id", (brand_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["disallowed_wording"] = json.loads(d["disallowed_wording"] or "[]")
            out.append(d)
        return out

    def add_fact_snapshot(self, brand_id: int, snapshot_hash: str, facts_version: int, content: str) -> None:
        self._conn.execute(
            "INSERT INTO fact_snapshots (brand_id, snapshot_hash, facts_version, content, imported_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (brand_id, snapshot_hash, facts_version, content, utcnow()),
        )
        self._conn.commit()

    def latest_fact_snapshot(self, brand_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self._conn.execute(
                "SELECT * FROM fact_snapshots WHERE brand_id = ? ORDER BY id DESC LIMIT 1", (brand_id,)
            ).fetchone()
        )

    def latest_facts_payload(self, brand_id: int) -> dict[str, Any] | None:
        """Full FactsYaml payload (rules + material_terms) of the latest snapshot,
        annotated with the snapshot's facts_hash."""
        snapshot = self.latest_fact_snapshot(brand_id)
        if snapshot is None:
            return None
        payload = json.loads(snapshot["content"])
        payload["facts_hash"] = snapshot["snapshot_hash"]
        return payload

    # ---- policy ----

    def set_policy(self, brand_id: int, policy_json: str, policy_hash: str) -> None:
        self._conn.execute(
            "INSERT INTO brand_policies (brand_id, policy_json, policy_hash, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(brand_id) DO UPDATE SET policy_json = excluded.policy_json,"
            " policy_hash = excluded.policy_hash, updated_at = excluded.updated_at",
            (brand_id, policy_json, policy_hash, utcnow()),
        )
        self._conn.commit()

    def get_policy(self, brand_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM brand_policies WHERE brand_id = ?", (brand_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["policy_json"])

    def runs_with_stale_policy(self, brand_id: int, policy_hash: str) -> list[dict[str, Any]]:
        """Runs whose brief was created under an older policy hash."""
        rows = self._conn.execute(
            "SELECT DISTINCT r.id, r.status, r.step FROM runs r JOIN projects p ON p.id = r.project_id"
            " WHERE p.brand_id = ? AND r.status IN ('approved', 'drafting', 'completed')"
            " AND json_extract(r.brief_snapshot, '$.policy_hash') IS NOT NULL"
            " AND json_extract(r.brief_snapshot, '$.policy_hash') != ?",
            (brand_id, policy_hash),
        ).fetchall()
        return [dict(r) for r in rows]

    def brands_with_stale_approvals(self, brand_id: int, facts_hash: str) -> list[dict[str, Any]]:
        """Runs of this brand whose approvals reference an older facts hash."""
        rows = self._conn.execute(
            "SELECT DISTINCT r.id, r.status, r.step FROM runs r JOIN projects p ON p.id = r.project_id"
            " WHERE p.brand_id = ? AND r.status IN ('approved', 'drafting', 'completed')"
            " AND r.facts_hash != ?",
            (brand_id, facts_hash),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- GSC (Google Search Console) ----

    def upsert_gsc_property(
        self, brand: str, property_url: str, auth_path: str, client_json_path: str | None = None,
        *, status: str = "connected",
    ) -> dict[str, Any]:
        self._conn.execute(
            "INSERT INTO gsc_properties (brand, property_url, auth_path, client_json_path, status)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(brand) DO UPDATE SET property_url = excluded.property_url,"
            " auth_path = excluded.auth_path, client_json_path = excluded.client_json_path,"
            " status = excluded.status",
            (brand, property_url, auth_path, client_json_path, status),
        )
        self._conn.commit()
        return {
            "brand": brand,
            "property_url": property_url,
            "auth_path": auth_path,
            "client_json_path": client_json_path,
            "status": status,
        }

    def get_gsc_property(self, brand: str) -> dict[str, Any] | None:
        return _row_to_dict(
            self._conn.execute("SELECT * FROM gsc_properties WHERE brand = ?", (brand,)).fetchone()
        )

    def update_gsc_property_synced(self, brand: str, at: str) -> None:
        self._conn.execute(
            "UPDATE gsc_properties SET last_synced_at = ?,"
            " status = CASE WHEN auth_path IN ('gcloud-adc', 'own-client')"
            " THEN 'connected' ELSE status END WHERE brand = ?",
            (at, brand),
        )
        self._conn.commit()

    def upsert_gsc_query_rows(self, property_url: str, rows: list[dict[str, Any]]) -> int:
        for r in rows:
            self._conn.execute(
                "INSERT INTO gsc_queries (property_url, data_date, query, page, device, country,"
                " search_type, clicks, impressions, ctr, position, pulled_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(property_url, data_date, query, page, device, country, search_type)"
                " DO UPDATE SET clicks = excluded.clicks, impressions = excluded.impressions,"
                " ctr = excluded.ctr, position = excluded.position, pulled_at = excluded.pulled_at",
                (
                    property_url,
                    r["data_date"],
                    r["query"],
                    r.get("page") or "",
                    r.get("device") or "",
                    r.get("country") or "",
                    r.get("search_type", "web"),
                    r["clicks"],
                    r["impressions"],
                    r["ctr"],
                    r["position"],
                    r["pulled_at"],
                ),
            )
        self._conn.commit()
        return len(rows)

    def gsc_query_rows(self, property_url: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM gsc_queries WHERE property_url = ? AND data_date BETWEEN ? AND ?"
                " ORDER BY data_date, query",
                (property_url, start_date, end_date),
            ).fetchall()
        ]

    def mark_gsc_pull_complete(self, property_url: str, dimension: str, data_date: str) -> None:
        self._conn.execute(
            "INSERT INTO gsc_pull_state (property_url, dimension, data_date, completed_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(property_url, dimension, data_date) DO NOTHING",
            (property_url, dimension, data_date, utcnow()),
        )
        self._conn.commit()

    def gsc_pull_complete(self, property_url: str, dimension: str, data_date: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM gsc_pull_state WHERE property_url = ? AND dimension = ? AND data_date = ?",
            (property_url, dimension, data_date),
        ).fetchone()
        return row is not None

    def upsert_gsc_inspection(self, row: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO gsc_inspections (property_url, url, inspected_at, index_status, mobile_usable,"
            " last_crawl) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(property_url, url) DO UPDATE SET inspected_at = excluded.inspected_at,"
            " index_status = excluded.index_status, mobile_usable = excluded.mobile_usable,"
            " last_crawl = excluded.last_crawl",
            (
                row["property_url"],
                row["url"],
                row["inspected_at"],
                row.get("index_status"),
                row.get("mobile_usable"),
                row.get("last_crawl"),
            ),
        )
        self._conn.commit()

    def gsc_sync_range(self, property_url: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT MIN(data_date) AS start_date, MAX(data_date) AS end_date,"
            " COUNT(DISTINCT data_date) AS days FROM gsc_queries WHERE property_url = ?",
            (property_url,),
        ).fetchone()
        if row is None or row["start_date"] is None:
            return None
        return {
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "days": row["days"],
        }
