"""Deterministic ids and idempotency keys (no random state in core logic)."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_payload(payload: object) -> str:
    return sha256_text(json.dumps(payload, sort_keys=True, default=str))


def new_run_id() -> str:
    return f"run-{secrets.token_hex(8)}"


def idempotency_key(run_id: str, step: str, payload: object = None) -> str:
    """Deterministic default key: same run+step+inputs -> same key.

    A prior successful ledger entry with this key short-circuits re-execution
    (no duplicate provider calls, cost rows or artifacts).
    """
    suffix = f":{hash_payload(payload)[:12]}" if payload is not None else ""
    return f"run:{run_id}:{step}{suffix}"
