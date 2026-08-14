"""Shared CLI state and helpers for the seo-writer command package.

Kept out of ``cli/__init__.py`` so the per-group command modules can import
these helpers without a circular import through the package root.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from .. import gsc, onboard, services
from ..config import Workspace, ensure_workspace, resolve_data_dir, resolve_workspace
from ..db import Database
from ..errors import ProviderError, SeoWriterError, UsageError, ValidationFailedError


class Ctx:
    data_dir: Path = resolve_data_dir()
    workspace: str = resolve_workspace()
    json: bool = False


state = Ctx()


def _open() -> tuple[Workspace, Database]:
    ws = ensure_workspace(state.data_dir, state.workspace)
    return ws, ws.open_db()


def _guard(fn: Callable[[], Any]) -> Any:
    """Run a command body; map SeoWriterError to the exit-code contract."""
    try:
        result = fn()
    except SeoWriterError as exc:
        if state.json:
            payload: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, ValidationFailedError):
                payload.update({"step": exc.step, "reasons": exc.reasons})
            if isinstance(exc, ProviderError):
                payload.update({"provider": exc.provider, "retryable": exc.retryable})
            if isinstance(exc, gsc.GscError):
                payload.update({"retryable": exc.retryable})
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        raise typer.Exit(code=exc.exit_code) from None
    if result is not None:
        if state.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            _render(result)
    return result


def _render(value: Any, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                _render(v, indent + 1)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                _render(item, indent)
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{value}")


def _run_context(db: Database, run_id: str) -> tuple[dict, dict, dict]:
    """Resolve run + its brand + the brand policy from a run id."""
    run = services.resolve_run(db, run_id)
    brief = json.loads(run["brief_snapshot"])
    brand = services.resolve_brand(db, brief["brand_slug"])
    policy = services.load_policy(db, brand["id"])
    return run, brand, policy


def _require_both(brand: str | None, project: str | None) -> None:
    if (brand is None) != (project is None):
        raise UsageError("--brand and --project must be given together")


_PROVIDER_ENV_HINTS = {
    "dataforseo.login": "DATAFORSEO_LOGIN",
    "dataforseo.password": "DATAFORSEO_PASSWORD",
    "reddit.client_id": "REDDIT_CLIENT_ID",
    "reddit.client_secret": "REDDIT_CLIENT_SECRET",
}


def _prompt_provider_values(prov: str) -> dict[str, str]:
    if state.json:
        raise UsageError("interactive setup needs a terminal; run without --json or set provider env vars")
    values: dict[str, str] = {}
    for key, label in onboard.PROVIDERS[prov].items():
        is_secret = key in ("password", "client_secret")
        env_name = _PROVIDER_ENV_HINTS.get(f"{prov}.{key}")
        env_hint = os.environ.get(env_name or "", "") if env_name else ""
        prompt_text = f"{prov} {label}" + (" (enter = env)" if env_hint else "")
        val = typer.prompt(prompt_text, default="", hide_input=is_secret)
        values[key] = val if val else env_hint
    return values


def _gsc_prompt(message: str) -> str:
    """Interactive prompt; refused under --json (the CLI stays scriptable)."""
    if state.json:
        raise UsageError("interactive setup needs a terminal; drop --json or pass the required options")
    return typer.prompt(message)
