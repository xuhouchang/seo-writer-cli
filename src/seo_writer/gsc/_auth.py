"""Credentials: gcloud ADC / own-client handling, paths, token storage.

Credentials and data stay on the customer's machine: the gcloud ADC file is
read in place, a self-built client writes a chmod-600 client json + token
file under the workspace. Secrets never enter git, logs, or error messages —
audit events only carry property references and file paths.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import gsc as _gsc
from ..config import Workspace
from ..db import Database
from ..errors import UsageError
from ._constants import GCLOUD_SCOPE_ARG
from ._errors import GscError


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
    adc = Path(path) if path is not None else _gsc.GCLOUD_ADC_PATH
    if not adc.exists():
        raise GscError(
            f"gcloud ADC credentials not found at {adc}. "
            f'Run: gcloud auth application-default login --scopes="{GCLOUD_SCOPE_ARG}"'
        )
    try:
        payload = json.loads(adc.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GscError(f"cannot read gcloud ADC file {adc}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "authorized_user":
        raise GscError(
            f"{adc} is not an authorized_user credential (type={payload.get('type')!r}); "
            f're-run gcloud auth application-default login --scopes="{GCLOUD_SCOPE_ARG}"'
        )
    client_id = str(payload.get("client_id") or "")
    client_secret = str(payload.get("client_secret") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    if not client_id or not client_secret or not refresh_token:
        raise GscError(
            f"{adc} is missing client_id/client_secret/refresh_token; "
            f're-run gcloud auth application-default login --scopes="{GCLOUD_SCOPE_ARG}"'
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
    if _gsc.GCLOUD_ADC_PATH.exists():
        return load_adc(), prop
    if client_json_path(ws, brand).exists():
        return load_client_json(ws, brand), prop
    raise GscError(
        f"brand '{brand}' has no GSC credentials configured; "
        f"run `seo-writer gsc setup --brand {brand}` first"
    )
