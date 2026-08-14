"""HTTP transport for the Google Search Console integration.

Standard library only (urllib) — no google-api-python-client, no requests,
no google-auth-oauthlib. Covers the raw request helper, Google error
parsing, access-token refresh, scope verification and the small shared
API request helpers used by pull / inspect / csv-import.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import gsc as _gsc
from ..errors import UsageError
from ._auth import Credentials
from ._constants import SCOPE
from ._errors import GscAuthError, GscError, GscQuotaError, GscTransientError


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
    token_url = token_url or _gsc.TOKEN_URL
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
        }
    ).encode("utf-8")
    status, body = _gsc._http_request(
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
    tokeninfo_url = tokeninfo_url or _gsc.TOKENINFO_URL
    status, body = _gsc._http_request(
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
    has_readonly = SCOPE in scopes
    has_write = "https://www.googleapis.com/auth/webmasters" in scopes
    return {"scopes": scopes, "has_readonly": has_readonly, "has_webmasters": has_readonly or has_write}


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
    status, raw = _gsc._http_request(method, url, headers=headers, body=data, timeout=timeout)
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
