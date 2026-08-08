"""Workspace resolution and YAML configuration loading.

Default data directory is ~/.seo-writer/<workspace>/ with a SQLite database
plus an objects/ tree for run artifacts. Overridable via SEO_WRITER_DATA_DIR
and SEO_WRITER_WORKSPACE (or CLI flags) for isolation and tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from .db import Database
from .errors import UsageError
from .ids import hash_payload

DEFAULT_DATA_DIR = Path.home() / ".seo-writer"


def resolve_data_dir(cli_value: str | None = None) -> Path:
    value = cli_value or os.environ.get("SEO_WRITER_DATA_DIR")
    return Path(value).expanduser() if value else DEFAULT_DATA_DIR


def resolve_workspace(cli_value: str | None = None) -> str:
    return cli_value or os.environ.get("SEO_WRITER_WORKSPACE") or "default"


class Workspace:
    def __init__(self, data_dir: Path, slug: str) -> None:
        self.data_dir = Path(data_dir)
        self.slug = slug
        self.root = self.data_dir / slug
        self.db_path = self.root / "seo-writer.db"
        self.objects_dir = self.root / "objects"

    def open_db(self) -> Database:
        return Database(self.db_path)

    def run_dir(self, run_id: str) -> Path:
        path = self.objects_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, run_id: str, name: str, payload: object) -> Path:
        path = self.run_dir(run_id) / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def write_text(self, run_id: str, name: str, content: str) -> Path:
        path = self.run_dir(run_id) / name
        path.write_text(content, encoding="utf-8")
        return path


def ensure_workspace(data_dir: Path, slug: str) -> Workspace:
    ws = Workspace(data_dir, slug)
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.open_db().close()
    return ws


def load_yaml_model[M: BaseModel](path: Path, model: type[M], label: str) -> M:
    if not path.exists():
        raise UsageError(f"{label} file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise UsageError(f"{label} is not valid YAML: {exc}") from exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise UsageError(f"{label} validation failed: {details}") from exc


def facts_payload_hash(payload: dict) -> str:
    return hash_payload(payload)
