"""Auth flows: setup guide, PKCE desktop flow, gcloud ADC login, loopback receiver."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import gsc as _gsc
from ..config import Workspace
from ..db import Database
from ._auth import (
    client_json_path,
    import_client_json,
    load_adc,
    load_client_json,
    save_token,
    token_file_path,
)
from ._constants import (
    AUTH_TIMEOUT_S,
    AUTH_URL,
    GCLOUD_BIN,
    GCLOUD_SCOPE_ARG,
    GCLOUD_SCOPES,
    SCOPE,
)
from ._errors import GscAuthError, GscError
from ._http import _raise_for_status, verify_credentials


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
    adc = adc_path or _gsc.GCLOUD_ADC_PATH
    if adc.exists():
        try:
            creds = load_adc(adc)
            verification = verify_credentials(creds, token_url=token_url, tokeninfo_url=tokeninfo_url)
        except GscAuthError as exc:
            return {
                "status": "adc-invalid",
                "adc_file": str(adc),
                "message": str(exc),
                "next": f're-run: gcloud auth application-default login --scopes="{GCLOUD_SCOPE_ARG}"',
            }
        if not verification["has_webmasters"]:
            return {
                "status": "adc-missing-scope",
                "adc_file": str(adc),
                "message": verification["message"],
                "next": f're-run: gcloud auth application-default login --scopes="{GCLOUD_SCOPE_ARG}"',
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
                "title": "gcloud ADC（默认由交付人员协助完成一次 Google 授权）",
                "steps": [
                    "install the Google Cloud SDK once (we do this during delivery)",
                    f'gcloud auth application-default login --scopes="{GCLOUD_SCOPE_ARG}"',
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
    token_url = token_url or _gsc.TOKEN_URL
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
    status, body = _gsc._http_request(
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
    cmd = [gcloud_bin, "auth", "application-default", "login", "--scopes", ",".join(GCLOUD_SCOPES)]
    if no_launch_browser:
        cmd.append("--no-launch-browser")
    proc = run(cmd, capture_output=False, text=True, timeout=300)
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
        auth_path is None and client_json_path(ws, brand).exists() and not _gsc.GCLOUD_ADC_PATH.exists()
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
