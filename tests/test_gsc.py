"""GSC integration tests: credentials, setup, auth, pull, inspect, CSV, insights.

Everything runs offline against synthetic credentials and a local stub
server that emulates the token/tokeninfo endpoints and the Search Console
API. No real customer data anywhere — all rows are generated fixtures.

The stub is a plain http.server on 127.0.0.1:<free port>; behavior is driven
by class attributes so each test declares its own failure modes (revoked
token, quota 429s, pagination totals, …). The server also records every
request for idempotency/retry assertions.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from seo_writer import gsc
from seo_writer.config import ensure_workspace
from seo_writer.db import Database
from seo_writer.errors import UsageError
from tests.conftest import run_json_cli

PROPERTY = "https://www.example.com/"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
CLIENT_ID = "synthetic-client.apps.googleusercontent.com"
CLIENT_SECRET = "synthetic_client_secret_never_real"
REFRESH_TOKEN = "synthetic_refresh_token_never_real"

ADC_PAYLOAD = {
    "type": "authorized_user",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "refresh_token": REFRESH_TOKEN,
    "quota_project_id": "synthetic-quota-project",
}

CLIENT_JSON = {
    "installed": {
        "client_id": CLIENT_ID,
        "project_id": "synthetic-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": CLIENT_SECRET,
        "redirect_uris": ["http://localhost"],
    }
}


# ---------------------------------------------------------------------------
# stub server
# ---------------------------------------------------------------------------


AUTH_ERROR_PAYLOAD = {
    "error": {
        "status": "UNAUTHENTICATED",
        "message": (
            "Request had invalid authentication credentials. Expected OAuth 2 access token..."
        ),
    }
}


def _sa_rows(day: str, dims: list[str], start_row: int, count: int) -> list[dict]:
    rows = []
    for i in range(count):
        if dims[1] == "query":
            second = f"query {start_row + i}"
        else:
            second = f"https://www.example.com/page{start_row + i}"
        rows.append(
            {"keys": [day, second], "clicks": i + 1, "impressions": 100, "ctr": 0.01, "position": 10}
        )
    return rows


class _GscStub(BaseHTTPRequestHandler):
    """Emulates oauth2 token/tokeninfo + Search Console v3/v1 endpoints."""

    token_status = 200
    token_payload = {"access_token": "x", "expires_in": 3600}
    tokeninfo_payload = {"scope": SCOPE}
    sites_payload = {"siteEntry": [{"siteUrl": PROPERTY, "permissionLevel": "siteFullUser"}]}
    sa_total = 3
    sa_page = 3
    sa_429_remaining = 0
    sa_query_count = 0  # 1-based counter over searchAnalytics/query calls
    sa_401_on_call = 0  # 0 = disabled; otherwise 401 on this call index (expired token)
    sa_401_remaining = 0  # 0 = disabled; otherwise this many consecutive 401s
    inspect_payload = {
        "inspectionResult": {
            "indexStatusResult": {
                "coverageState": "SUBMITTED_AND_INDEXED",
                "lastCrawlTime": "2026-07-01T00:00:00Z",
            },
            "mobileUsabilityResult": {"verdict": "MOBILE_USABLE"},
        }
    }
    requests: list[tuple[str, str, dict | None]] = []
    headers_seen: list[dict[str, str]] = []

    def _record(self, body: bytes | None = None) -> None:
        parsed: dict | str | None = None
        if body:
            text = body.decode("utf-8")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {k: v[0] if len(v) == 1 else v for k, v in urllib.parse.parse_qs(text).items()}
        _GscStub.requests.append((self.command, self.path, parsed))
        _GscStub.headers_seen.append(dict(self.headers))

    def _send(self, status: int, payload: dict | None = None) -> None:
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/tokeninfo":
            self._record()
            self._send(200, _GscStub.tokeninfo_payload)
        elif path == "/webmasters/v3/sites":
            self._record()
            self._send(200, _GscStub.sites_payload)
        else:
            self._record()
            self._send(404, {"error": {"message": f"stub: no GET route {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        path = urllib.parse.urlparse(self.path).path
        if path == "/token":
            self._record(body)
            self._send(_GscStub.token_status, _GscStub.token_payload)
        elif path == "/v1/urlInspection/index:inspect":
            self._record(body)
            self._send(200, _GscStub.inspect_payload)
        elif path.endswith("/searchAnalytics/query"):
            self._record(body)
            _GscStub.sa_query_count += 1
            if _GscStub.sa_401_on_call and _GscStub.sa_query_count == _GscStub.sa_401_on_call:
                self._send(401, AUTH_ERROR_PAYLOAD)
                return
            if _GscStub.sa_401_remaining > 0:
                _GscStub.sa_401_remaining -= 1
                self._send(401, AUTH_ERROR_PAYLOAD)
                return
            if _GscStub.sa_429_remaining > 0:
                _GscStub.sa_429_remaining -= 1
                self._send(429, {"error": {"message": "Quota exceeded for quota metric 'Queries'."}})
                return
            payload = json.loads(body.decode("utf-8"))
            day = payload["startDate"]
            dims = payload["dimensions"]
            start_row = payload["startRow"]
            count = min(_GscStub.sa_page, max(0, _GscStub.sa_total - start_row))
            rows = _sa_rows(day, dims, start_row, count)
            self._send(200, {"rows": rows, "totalMatches": _GscStub.sa_total})
        else:
            self._record(body)
            self._send(404, {"error": {"message": f"stub: no POST route {path}"}})

    def log_message(self, *args: object) -> None:  # silence test noise
        pass


@pytest.fixture
def stub(monkeypatch):
    _GscStub.token_status = 200
    _GscStub.token_payload = {"access_token": "x", "expires_in": 3600}
    _GscStub.tokeninfo_payload = {"scope": SCOPE}
    _GscStub.sites_payload = {"siteEntry": [{"siteUrl": PROPERTY, "permissionLevel": "siteFullUser"}]}
    _GscStub.sa_total = 3
    _GscStub.sa_page = 3
    _GscStub.sa_429_remaining = 0
    _GscStub.sa_query_count = 0
    _GscStub.sa_401_on_call = 0
    _GscStub.sa_401_remaining = 0
    _GscStub.inspect_payload = {
        "inspectionResult": {
            "indexStatusResult": {
                "coverageState": "SUBMITTED_AND_INDEXED",
                "lastCrawlTime": "2026-07-01T00:00:00Z",
            },
            "mobileUsabilityResult": {"verdict": "MOBILE_USABLE"},
        }
    }
    _GscStub.requests = []
    _GscStub.headers_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GscStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(gsc, "TOKEN_URL", f"{server.url}/token")
    monkeypatch.setattr(gsc, "TOKENINFO_URL", f"{server.url}/tokeninfo")
    monkeypatch.setattr(gsc, "API_BASE", server.url)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def adc_file(tmp_path: Path) -> Path:
    path = tmp_path / "adc.json"
    path.write_text(json.dumps(ADC_PAYLOAD), encoding="utf-8")
    return path


@pytest.fixture
def client_file(tmp_path: Path) -> Path:
    path = tmp_path / "client.json"
    path.write_text(json.dumps(CLIENT_JSON), encoding="utf-8")
    return path


@pytest.fixture
def connected(ws, db, monkeypatch, stub, adc_file) -> None:
    """Brand with a verified gcloud-adc credential + connected property."""

    def load(_db, _ws, _brand) -> tuple[gsc.Credentials, dict | None]:
        return gsc.load_adc(adc_file), _db.get_gsc_property(_brand)

    monkeypatch.setattr(gsc, "GCLOUD_ADC_PATH", adc_file)
    monkeypatch.setattr(gsc, "load_credentials", load)
    db.upsert_gsc_property("acme", PROPERTY, "gcloud-adc", None)


# ---------------------------------------------------------------------------
# T1 — credential layer (offline fixtures)
# ---------------------------------------------------------------------------


def test_load_adc_happy(adc_file):
    creds = gsc.load_adc(adc_file)
    assert creds.client_id == CLIENT_ID
    assert creds.client_secret == CLIENT_SECRET
    assert creds.refresh_token == REFRESH_TOKEN
    assert creds.auth_type == "gcloud-adc"
    assert creds.client_json_path == str(adc_file)


def test_load_adc_missing_file(tmp_path):
    with pytest.raises(gsc.GscError) as exc:
        gsc.load_adc(tmp_path / "nope.json")
    assert "gcloud ADC credentials not found" in str(exc.value)
    assert not exc.value.retryable


def test_load_adc_wrong_type(tmp_path):
    path = tmp_path / "adc.json"
    path.write_text(json.dumps({"type": "service_account", "client_id": CLIENT_ID}), encoding="utf-8")
    with pytest.raises(gsc.GscError):
        gsc.load_adc(path)


def test_load_adc_missing_fields(tmp_path):
    path = tmp_path / "adc.json"
    path.write_text(json.dumps({"type": "authorized_user", "client_id": CLIENT_ID}), encoding="utf-8")
    with pytest.raises(gsc.GscError) as exc:
        gsc.load_adc(path)
    assert "missing client_id/client_secret/refresh_token" in str(exc.value)


def test_load_adc_corrupt_json(tmp_path):
    path = tmp_path / "adc.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(gsc.GscError):
        gsc.load_adc(path)


def test_refresh_access_token_success(stub):
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    token = gsc.refresh_access_token(creds, token_url=f"{stub.url}/token")
    assert token == "x"
    method, path, body = _GscStub.requests[-1]
    assert method == "POST" and path == "/token"
    assert body["grant_type"] == "refresh_token"
    assert body["client_id"] == CLIENT_ID


def test_refresh_access_token_invalid_grant_is_permanent(stub):
    _GscStub.token_status = 400
    _GscStub.token_payload = {
        "error": "invalid_grant",
        "error_description": "Token has been expired or revoked.",
    }
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    with pytest.raises(gsc.GscAuthError) as exc:
        gsc.refresh_access_token(creds, token_url=f"{stub.url}/token")
    assert "revoked or expired" in str(exc.value)
    assert not exc.value.retryable


def test_refresh_access_token_invalid_client(stub):
    _GscStub.token_status = 400
    _GscStub.token_payload = {"error": "invalid_client", "error_description": "Unauthorized"}
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    with pytest.raises(gsc.GscAuthError) as exc:
        gsc.refresh_access_token(creds, token_url=f"{stub.url}/token")
    assert "client credentials were rejected" in str(exc.value)


def test_refresh_access_token_invalid_scope(stub):
    _GscStub.token_status = 400
    _GscStub.token_payload = {"error": "invalid_scope", "error_description": "Bad OAuth scope"}
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    with pytest.raises(gsc.GscAuthError) as exc:
        gsc.refresh_access_token(creds, token_url=f"{stub.url}/token")
    assert "webmasters scope is missing" in str(exc.value)


def test_refresh_access_token_network_error_is_retryable(monkeypatch):
    def boom(*args, **kwargs):
        raise gsc.GscTransientError("network error for http://nonexistent: connect failed")

    monkeypatch.setattr(gsc, "_http_request", boom)
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    with pytest.raises(gsc.GscTransientError) as exc:
        gsc.refresh_access_token(creds, token_url="http://nonexistent/token")
    assert exc.value.retryable


def test_refresh_access_token_quota_429_is_retryable(stub):
    _GscStub.token_status = 429
    _GscStub.token_payload = {"error": {"message": "Quota exceeded."}}
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    with pytest.raises(gsc.GscQuotaError) as exc:
        gsc.refresh_access_token(creds, token_url=f"{stub.url}/token")
    assert exc.value.retryable


def test_refresh_access_token_without_refresh_token_is_permanent():
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, None, "own-client")
    with pytest.raises(gsc.GscAuthError) as exc:
        gsc.refresh_access_token(creds)
    assert not exc.value.retryable


def test_check_scope_has_webmasters(stub):
    result = gsc.check_scope("x", tokeninfo_url=f"{stub.url}/tokeninfo")
    assert result["has_readonly"] is True
    assert result["has_webmasters"] is True
    assert SCOPE in result["scopes"]


def test_check_scope_missing_webmasters(stub):
    _GscStub.tokeninfo_payload = {"scope": "https://www.googleapis.com/auth/drive.readonly"}
    result = gsc.check_scope("x", tokeninfo_url=f"{stub.url}/tokeninfo")
    assert result["has_readonly"] is False
    assert result["has_webmasters"] is False


def test_check_scope_does_not_accept_unrelated_webmasters_scope(stub):
    _GscStub.tokeninfo_payload = {"scope": "https://www.googleapis.com/auth/webmasters.foo"}
    result = gsc.check_scope("x", tokeninfo_url=f"{stub.url}/tokeninfo")
    assert result["has_readonly"] is False
    assert result["has_webmasters"] is False


def test_verify_credentials_ok(stub):
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    result = gsc.verify_credentials(
        creds, token_url=f"{stub.url}/token", tokeninfo_url=f"{stub.url}/tokeninfo"
    )
    assert result["ok"] is True and result["has_webmasters"] is True


def test_verify_credentials_missing_scope(stub):
    _GscStub.tokeninfo_payload = {"scope": "https://www.googleapis.com/auth/drive.readonly"}
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    result = gsc.verify_credentials(
        creds, token_url=f"{stub.url}/token", tokeninfo_url=f"{stub.url}/tokeninfo"
    )
    assert result["ok"] is False and result["has_webmasters"] is False
    assert "webmasters scope is missing" in result["message"]


def test_credentials_secrets_never_in_error_messages(stub):
    _GscStub.token_status = 400
    _GscStub.token_payload = {"error": "invalid_grant", "error_description": "Token revoked."}
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    with pytest.raises(gsc.GscAuthError) as exc:
        gsc.refresh_access_token(creds, token_url=f"{stub.url}/token")
    message = str(exc.value)
    assert CLIENT_SECRET not in message
    assert REFRESH_TOKEN not in message
    assert "ya29." not in message


# ---------------------------------------------------------------------------
# T2 — setup (path detection, A/B guidance, client json import)
# ---------------------------------------------------------------------------


def test_setup_adc_ready(ws, db, stub, adc_file):
    result = gsc.setup_guide(
        db,
        ws,
        "acme",
        adc_path=adc_file,
        token_url=f"{stub.url}/token",
        tokeninfo_url=f"{stub.url}/tokeninfo",
    )
    assert result["status"] == "ready"
    assert result["auth_path"] == "gcloud-adc"
    assert result["verification"]["has_webmasters"] is True


def test_setup_adc_invalid(stub, tmp_path):
    ws = ensure_workspace(tmp_path / "data", "test")
    db = ws.open_db()
    adc = tmp_path / "adc.json"
    adc.write_text(json.dumps(ADC_PAYLOAD), encoding="utf-8")
    _GscStub.token_status = 400
    _GscStub.token_payload = {"error": "invalid_grant", "error_description": "Token revoked."}
    result = gsc.setup_guide(
        db, ws, "acme", adc_path=adc, token_url=f"{stub.url}/token", tokeninfo_url=f"{stub.url}/tokeninfo"
    )
    assert result["status"] == "adc-invalid"
    assert "re-run" in result["next"]


def test_setup_no_credentials_offers_paths(ws, db, tmp_path):
    result = gsc.setup_guide(db, ws, "acme", adc_path=tmp_path / "missing.json", interactive=False)
    assert result["status"] == "no-credentials"
    assert [o["path"] for o in result["options"]] == ["A", "B"]
    assert "gcloud" in result["options"][0]["steps"][1]
    assert result["options"][0]["title"] == "gcloud ADC（默认由交付人员协助完成一次 Google 授权）"
    assert "never touches GCP" not in json.dumps(result)


def test_readme_matches_gsc_pull_contract():
    readme = Path(__file__).parents[1].joinpath("README.md").read_text(encoding="utf-8")
    assert "[--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD]" in readme
    assert "--days" not in readme
    assert "--date" not in readme
    assert "SEO-WRITER-GSC-PLAN" not in readme


def test_import_client_json_validates_and_chmods(ws, db, client_file):
    result = gsc.import_client_json(ws, "acme", client_file)
    assert result["status"] == "client-imported"
    target = gsc.client_json_path(ws, "acme")
    assert target.exists()
    assert (target.stat().st_mode & 0o777) == 0o600


def test_import_client_json_missing_fields(ws, db, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"installed": {"client_id": CLIENT_ID}}), encoding="utf-8")
    with pytest.raises(gsc.GscError) as exc:
        gsc.import_client_json(ws, "acme", bad)
    assert "missing client_id/client_secret" in str(exc.value)


def test_import_client_json_not_a_dict(ws, db, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(gsc.GscError):
        gsc.import_client_json(ws, "acme", bad)


def test_import_client_json_missing_file(ws, db, tmp_path):
    with pytest.raises(UsageError):
        gsc.import_client_json(ws, "acme", tmp_path / "nope.json")


def test_setup_with_client_json_imports(ws, db, client_file):
    result = gsc.setup_guide(db, ws, "acme", client_json=str(client_file))
    assert result["status"] == "client-imported"
    assert gsc.client_json_path(ws, "acme").exists()


# ---------------------------------------------------------------------------
# T3 — auth (PKCE desktop flow, paste-code mode, gcloud path)
# ---------------------------------------------------------------------------


def test_pkce_pair_shapes():
    verifier, challenge = gsc.pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge


def test_build_auth_url_parameters():
    url = gsc.build_auth_url(CLIENT_ID, "http://127.0.0.1:9999/", "challenge123")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert query["client_id"] == CLIENT_ID
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == "challenge123"
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["scope"] == SCOPE


def test_exchange_code_returns_refresh_token(stub):
    _GscStub.token_payload = {
        "access_token": "ya29.code",
        "refresh_token": "synthetic_refresh_from_code",
        "scope": SCOPE,
        "expires_in": 3600,
    }
    result = gsc.exchange_code(
        CLIENT_ID,
        CLIENT_SECRET,
        "thecode",
        "theverifier",
        "http://127.0.0.1:9999/",
        token_url=f"{stub.url}/token",
    )
    assert result["refresh_token"] == "synthetic_refresh_from_code"
    method, path, body = _GscStub.requests[-1]
    assert body["grant_type"] == "authorization_code"
    assert body["code_verifier"] == "theverifier"


def test_exchange_code_missing_refresh_token(stub):
    _GscStub.token_payload = {"access_token": "ya29.code", "expires_in": 3600}
    with pytest.raises(gsc.GscAuthError) as exc:
        gsc.exchange_code(
            CLIENT_ID, CLIENT_SECRET, "c", "v", "http://127.0.0.1:9999/", token_url=f"{stub.url}/token"
        )
    assert "access_type=offline" in str(exc.value)


def test_desktop_auth_paste_code_mode(ws, db, client_file, stub):
    _GscStub.token_payload = {
        "access_token": "ya29.code",
        "refresh_token": "synthetic_refresh_from_code",
        "scope": SCOPE,
        "expires_in": 3600,
    }
    gsc.import_client_json(ws, "acme", client_file)
    result = gsc.desktop_auth(
        ws, "acme", no_launch_browser=True, prompt=lambda _: "the-code", token_url=f"{stub.url}/token"
    )
    assert result["status"] == "authorized"
    assert result["auth_path"] == "own-client"
    token_path = gsc.token_file_path(ws, "acme")
    assert (token_path.stat().st_mode & 0o777) == 0o600
    stored = json.loads(token_path.read_text(encoding="utf-8"))
    assert stored["refresh_token"] == "synthetic_refresh_from_code"


def test_desktop_auth_loopback_mode(ws, db, client_file, stub):
    _GscStub.token_payload = {
        "access_token": "ya29.code",
        "refresh_token": "synthetic_refresh_from_code",
        "scope": SCOPE,
        "expires_in": 3600,
    }
    gsc.import_client_json(ws, "acme", client_file)
    launched: list[str] = []

    def fake_launch(url: str) -> None:
        launched.append(url)
        # the loopback port lives in the redirect_uri query param, not the URL authority
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        port = urllib.parse.urlparse(query["redirect_uri"][0]).port
        urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=loopback-code", timeout=5).read()

    result = gsc.desktop_auth(
        ws, "acme", timeout=5, launch=fake_launch, token_url=f"{stub.url}/token"
    )
    assert result["status"] == "authorized"
    assert launched, "auth URL must be handed to the launcher"
    assert "code_challenge" in launched[0] and "code_challenge_method=S256" in launched[0]


def test_gcloud_auth_success():
    def fake_run(cmd, capture_output, text, timeout):
        assert cmd[:4] == ["gcloud", "auth", "application-default", "login"]
        assert cmd[5] == ",".join([gsc.CLOUD_PLATFORM_SCOPE, gsc.SCOPE])
        assert capture_output is False
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = gsc.gcloud_auth(gcloud_bin="gcloud", run=fake_run)
    assert result["status"] == "authorized"
    assert result["auth_path"] == "gcloud-adc"


def test_gcloud_auth_failure():
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with pytest.raises(gsc.GscError):
        gsc.gcloud_auth(gcloud_bin="gcloud", run=fake_run)


def test_run_auth_routes_to_own_client(ws, db, client_file, stub, monkeypatch, tmp_path):
    _GscStub.token_payload = {
        "access_token": "ya29.code",
        "refresh_token": "synthetic_refresh_from_code",
        "scope": SCOPE,
        "expires_in": 3600,
    }
    gsc.import_client_json(ws, "acme", client_file)
    monkeypatch.setattr(gsc, "GCLOUD_ADC_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(gsc, "TOKEN_URL", f"{stub.url}/token")
    result = gsc.run_auth(
        db, ws, "acme", no_launch_browser=True, prompt=lambda _: "the-code"
    )
    assert result["auth_path"] == "own-client"


def test_run_auth_routes_to_gcloud(ws, db, adc_file, monkeypatch):
    monkeypatch.setattr(gsc, "GCLOUD_ADC_PATH", adc_file)
    fake_run = lambda *a, **kw: subprocess.CompletedProcess(list(a[0]), 0, "", "")  # noqa: E731
    result = gsc.run_auth(db, ws, "acme", gcloud_bin="gcloud", run=fake_run)
    assert result["auth_path"] == "gcloud-adc"


def test_receive_code_times_out():
    with pytest.raises(gsc.GscError) as exc:
        gsc.receive_code_via_loopback(0, timeout=0.2)
    assert "timed out" in str(exc.value)


# ---------------------------------------------------------------------------
# T4 — sites / connect / pull (idempotency, resume, pagination, backoff)
# ---------------------------------------------------------------------------


def test_list_sites(stub):
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    result = gsc.list_sites(creds, api_base=stub.url)
    assert result["sites"] == [{"site_url": PROPERTY, "permission_level": "siteFullUser"}]


def test_api_requests_carry_quota_project_header(stub, adc_file):
    """ADC quota_project_id must travel as X-Goog-User-Project on API calls."""
    creds = gsc.load_adc(adc_file)
    assert creds.quota_project_id == "synthetic-quota-project"
    gsc.list_sites(creds, api_base=stub.url)
    headers = _GscStub.headers_seen[-1]
    assert headers.get("X-Goog-User-Project") == "synthetic-quota-project"


def test_api_requests_without_quota_project_have_no_header(stub):
    creds = gsc.Credentials(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, "gcloud-adc")
    gsc.list_sites(creds, api_base=stub.url)
    assert "X-Goog-User-Project" not in _GscStub.headers_seen[-1]


def test_connect_property(ws, db, connected):
    result = gsc.connect_property(db, ws, "acme", PROPERTY)
    assert result["status"] == "connected"
    assert result["property"] == PROPERTY
    stored = db.get_gsc_property("acme")
    assert stored["auth_path"] == "gcloud-adc"
    events = [e for e in db.list_audit() if e["event_type"] == "gsc.connect"]
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["property"] == PROPERTY
    assert REFRESH_TOKEN not in json.dumps(payload)


def test_connect_property_invalid_property(ws, db):
    with pytest.raises(UsageError):
        gsc.connect_property(db, ws, "acme", "not-a-url")
    with pytest.raises(UsageError):
        gsc.connect_property(db, ws, "acme", "sc-domain:")
    assert db.get_gsc_property("acme") is None


def test_connect_property_not_in_account(ws, db, connected):
    with pytest.raises(gsc.GscError) as exc:
        gsc.connect_property(db, ws, "acme", "https://not-yours.example.com/")
    assert "not in this account" in str(exc.value)


def test_pull_basic(ws, db, connected, stub):
    result = gsc.pull_search_analytics(
        db, ws, "acme", start_date="2026-07-01", end_date="2026-07-02"
    )
    assert result["dates_total"] == 2
    assert result["dates_pulled"] == 4  # 2 dates × 2 dimensions
    assert result["api_calls"] == 4
    assert result["rows_written"] == 4 * 3
    assert db.gsc_pull_complete(PROPERTY, "date,query", "2026-07-01")
    assert db.gsc_pull_complete(PROPERTY, "date,page", "2026-07-02")


def test_csv_import_pull_does_not_upgrade_status(ws, db, monkeypatch, stub, adc_file):
    db.upsert_gsc_property("acme", PROPERTY, "csv-import", None, status="imported")

    def load(_db, _ws, _brand) -> tuple[gsc.Credentials, dict | None]:
        return gsc.load_adc(adc_file), _db.get_gsc_property(_brand)

    monkeypatch.setattr(gsc, "load_credentials", load)
    gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-07-01")

    assert db.get_gsc_property("acme")["status"] == "imported"
    status = gsc.gsc_status(db, ws, "acme")
    assert status["connected"] is False
    assert status["source"] == "csv-import"


def test_pull_is_idempotent_zero_api_calls(ws, db, connected, stub):
    gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-07-02")
    requests_before = len(_GscStub.requests)
    result = gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-07-02")
    assert result["dates_skipped"] == 4
    assert result["dates_pulled"] == 0
    assert result["api_calls"] == 0
    assert len(_GscStub.requests) == requests_before  # no new API traffic


def test_pull_force_repulls(ws, db, connected, stub):
    gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-07-01")
    before = len(_GscStub.requests)
    result = gsc.pull_search_analytics(
        db, ws, "acme", start_date="2026-07-01", end_date="2026-07-01", force=True
    )
    assert result["dates_skipped"] == 0
    assert result["dates_pulled"] == 2
    assert len(_GscStub.requests) > before


def test_pull_resumes_after_interruption(ws, db, connected, stub):
    # simulate an interrupted earlier run: dimension "date,query" done, "date,page" not
    db.mark_gsc_pull_complete(PROPERTY, "date,query", "2026-07-01")
    db.mark_gsc_pull_complete(PROPERTY, "date,query", "2026-07-02")
    result = gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-07-02")
    assert result["dates_skipped"] == 2
    assert result["dates_pulled"] == 2
    assert result["api_calls"] == 2  # only the missing dimension
    assert db.gsc_pull_complete(PROPERTY, "date,page", "2026-07-01")


def test_pull_refreshes_expired_token_mid_pull_no_duplicates(ws, db, connected, stub, monkeypatch):
    """A long-window pull that outlives the 1h access token recovers mid-run.

    The 401 lands on page 2 of day 1 — after page 1 already upserted rows —
    so this also proves the retry keeps the pull idempotent (no duplicate rows
    from re-fetching a page) and the day is still marked complete.
    """
    monkeypatch.setattr(gsc, "ROW_LIMIT", 10)
    _GscStub.sa_page = 10
    _GscStub.sa_total = 25
    _GscStub.sa_401_on_call = 2  # page 2 of (date,query) gets an expired-token 401
    tokens_before = sum(1 for _, p, _ in _GscStub.requests if p == "/token")
    result = gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-07-01")
    tokens_after = sum(1 for _, p, _ in _GscStub.requests if p == "/token")
    assert tokens_after == tokens_before + 2  # initial lazy refresh + one mid-pull refresh
    assert result["dates_pulled"] == 2
    assert result["api_calls"] == 7  # 3 pages × 2 dims + 1 retried page
    assert result["rows_written"] == 50
    rows = db.gsc_query_rows(PROPERTY, "2026-07-01", "2026-07-01")
    assert len(rows) == 50  # the retried page did not duplicate rows
    assert db.gsc_pull_complete(PROPERTY, "date,query", "2026-07-01")
    assert db.gsc_pull_complete(PROPERTY, "date,page", "2026-07-01")


def test_pull_repeated_401_after_refresh_raises(ws, db, connected, stub):
    """A refresh that still gets 401s (revoked access) must not loop forever."""
    _GscStub.sa_401_remaining = 99  # every SA call 401s even after the refresh
    with pytest.raises(gsc.GscAuthError):
        gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-07-01")


def test_pull_paginates_with_start_row(ws, db, connected, stub, monkeypatch):
    monkeypatch.setattr(gsc, "ROW_LIMIT", 10)
    _GscStub.sa_page = 10
    _GscStub.sa_total = 25
    result = gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-07-01")
    assert result["api_calls"] == 6  # 3 pages × 2 dimensions
    assert result["rows_written"] == 50
    rows = db.gsc_query_rows(PROPERTY, "2026-07-01", "2026-07-01")
    assert len(rows) == 50


def test_pull_backs_off_on_quota_then_succeeds(ws, db, connected, stub):
    _GscStub.sa_429_remaining = 2
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    result = gsc.pull_search_analytics(
        db, ws, "acme", start_date="2026-07-01", end_date="2026-07-01", sleep=fake_sleep
    )
    assert result["api_calls"] == 4  # two failed attempts + two successful calls
    assert len(sleeps) == 2  # two backoff sleeps for the 429s
    assert sleeps[0] < sleeps[1]  # exponential growth
    assert 0.5 <= sleeps[0] <= 1.5  # base ~1s with jitter
    assert sleeps[1] <= 4.0  # capped by base*2^1 + jitter


def test_pull_quota_exhaustion_raises_retryable(ws, db, connected, stub):
    _GscStub.sa_429_remaining = 99
    with pytest.raises(gsc.GscQuotaError) as exc:
        gsc.pull_search_analytics(
            db, ws, "acme", start_date="2026-07-01", end_date="2026-07-01", sleep=lambda s: None
        )
    assert exc.value.retryable


def test_pull_date_range_too_large(ws, db, connected):
    with pytest.raises(UsageError):
        gsc.pull_search_analytics(
            db, ws, "acme", start_date="2025-01-01", end_date="2026-08-01"
        )


def test_pull_invalid_dates(ws, db, connected):
    with pytest.raises(UsageError):
        gsc.pull_search_analytics(db, ws, "acme", start_date="2026-07-01", end_date="2026-06-01")


def test_default_pull_window():
    start, end = gsc.default_pull_window(today=date(2026, 8, 8))
    assert end == "2026-08-05"
    assert start == "2026-07-07"


def test_rate_limiter_paces():
    class Clock:
        def __init__(self) -> None:
            self.t = 100.0
            self.sleeps: list[float] = []

        def now(self) -> float:
            return self.t

        def sleep(self, s: float) -> None:
            self.sleeps.append(s)
            self.t += s

    clock = Clock()
    limiter = gsc.RateLimiter(min_interval=1.0, sleep=clock.sleep, now=clock.now)
    limiter.wait()  # first call: no pacing
    assert clock.sleeps == []
    limiter.wait()  # 0s elapsed → must wait full interval
    assert clock.sleeps == [1.0]
    clock.t += 0.7  # 1.7s since last call → wait only 0.3s
    limiter.wait()
    assert clock.sleeps[-1] == pytest.approx(0.3)


def test_with_backoff_exhausts_after_attempts():
    calls = []

    def failing():
        calls.append(1)
        raise gsc.GscQuotaError("quota")

    with pytest.raises(gsc.GscQuotaError):
        gsc.with_backoff(failing, attempts=3, base_delay=0.001, sleep=lambda s: None)
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# T5 — inspect + read-only boundary
# ---------------------------------------------------------------------------


def test_inspect_url(ws, db, connected, stub):
    result = gsc.inspect_url(db, ws, "acme", "https://www.example.com/new-article/")
    assert result["index_status"] == "Submitted and indexed"
    assert result["coverage_state"] == "SUBMITTED_AND_INDEXED"
    assert result["mobile_usable"] is True
    row = db._conn.execute("SELECT * FROM gsc_inspections").fetchone()
    assert row["index_status"] == "SUBMITTED_AND_INDEXED"
    assert row["last_crawl"] == "2026-07-01T00:00:00Z"


def test_inspect_url_invalid(ws, db, connected):
    with pytest.raises(UsageError):
        gsc.inspect_url(db, ws, "acme", "not-a-url")


def test_gsc_has_no_sitemap_submit_command():
    proc = subprocess.run(
        [sys.executable, "-m", "seo_writer", "gsc", "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert "sitemap" not in proc.stdout.lower()


def test_gsc_code_has_no_sitemap_put():
    assert not hasattr(gsc, "submit_sitemap")
    assert "do_" + "PUT" not in Path(__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("args", [[], ["--help"]])
def test_root_empty_command_and_help_exit_zero(args):
    proc = subprocess.run([sys.executable, "-m", "seo_writer", *args], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "Usage" in proc.stdout


# ---------------------------------------------------------------------------
# T6 — CSV import (path C fallback)
# ---------------------------------------------------------------------------


def test_import_csv_with_bom_and_quotes(ws, db, tmp_path):
    csv_text = (
        "﻿Date,Query,Page,Impressions,Clicks,CTR,Position\r\n"
        "2026-07-01,\"query one, with comma\",https://www.example.com/a,\"1,234\",12,1.2%,7.5\r\n"
        "2026-07-01,second query,https://www.example.com/b,500,3,0.6%,12.1\r\n"
    )
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8")  # content already carries the BOM char
    result = gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    assert result["rows_imported"] == 2
    rows = db.gsc_query_rows(PROPERTY, "2026-07-01", "2026-07-01")
    assert rows[0]["query"] == "query one, with comma"
    assert rows[0]["impressions"] == 1234
    assert rows[0]["clicks"] == 12
    assert abs(rows[0]["ctr"] - 0.012) < 1e-9
    assert abs(rows[0]["position"] - 7.5) < 1e-9


def test_import_csv_uses_each_row_date(ws, db, tmp_path):
    csv_text = "Date,Query,Page,Impressions,Clicks,CTR,Position\n2026-07-02,first,https://www.example.com/a,100,1,1.0%,4.0\n"
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8-sig")
    result = gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    assert result["data_date"] == "from file"
    assert db.gsc_query_rows(PROPERTY, "2026-07-02", "2026-07-02")[0]["data_date"] == "2026-07-02"


def test_import_csv_has_no_library_date_override_parameter():
    assert "data_date" not in inspect.signature(gsc.import_gsc_csv).parameters


def test_import_csv_no_property(ws, db, tmp_path):
    csv_text = "Query,Page,Impressions,Clicks,CTR,Position\nfirst,https://www.example.com/a,100,1,1.0%,4.0\n"
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8-sig")
    with pytest.raises(UsageError) as exc:
        gsc.import_gsc_csv(db, ws, "acme", path)
    assert "no GSC property" in str(exc.value)


def test_import_csv_no_query_or_page_column(ws, db, tmp_path):
    csv_text = "Impressions,Clicks,CTR\n100,1,1.0%\n"
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8-sig")
    with pytest.raises(UsageError) as exc:
        gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    assert "does not look like a GSC export" in str(exc.value)


def test_import_csv_needs_date(ws, db, tmp_path):
    csv_text = "Query,Impressions,Clicks,CTR,Position\nfirst,100,1,1.0%,4.0\n"
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8-sig")
    with pytest.raises(UsageError) as exc:
        gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    assert "Date" in str(exc.value)


def test_import_csv_bad_number_is_usage_error_with_line(ws, db, tmp_path):
    csv_text = (
        "Date,Query,Impressions,Clicks,CTR,Position\n"
        "2026-07-01,first,100,1,1.0%,4.0\n"
        "2026-07-02,second,12abc,5,2.0%,3.0\n"
    )
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8-sig")
    with pytest.raises(UsageError) as exc:
        gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    msg = str(exc.value)
    assert "line 3" in msg
    assert "Impressions" in msg and "12abc" in msg


def test_csv_import_binds_property_as_offline_and_is_idempotent(ws, db, tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(
        "Date,Query,Impressions,Clicks,CTR,Position\n2026-07-01,first,100,1,1.0%,4.0\n",
        encoding="utf-8-sig",
    )
    first = gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    second = gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    assert first["rows_imported"] == second["rows_imported"] == 1
    assert db.get_gsc_property("acme")["status"] == "imported"
    assert db.get_gsc_property("acme")["auth_path"] == "csv-import"
    assert db._conn.execute("SELECT COUNT(*) FROM gsc_queries").fetchone()[0] == 1


def test_csv_import_rejects_property_conflict_without_writing(ws, db, tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(
        "Date,Query,Impressions,Clicks,CTR,Position\n2026-07-01,first,100,1,1.0%,4.0\n",
        encoding="utf-8-sig",
    )
    gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    with pytest.raises(UsageError, match="already bound"):
        gsc.import_gsc_csv(db, ws, "acme", path, property_url="https://other.example/")
    assert db._conn.execute("SELECT COUNT(*) FROM gsc_queries").fetchone()[0] == 1


def test_upsert_normalizes_nullable_dimensions_and_updates(ws, db):
    row = {"data_date": "2026-07-01", "query": "q", "page": None, "device": None, "country": None,
           "search_type": "web", "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 5.0,
           "pulled_at": "2026-08-01T00:00:00"}
    db.upsert_gsc_query_rows(PROPERTY, [row])
    row.update(clicks=2, impressions=20, pulled_at="2026-08-02T00:00:00")
    db.upsert_gsc_query_rows(PROPERTY, [row])
    stored = db._conn.execute("SELECT * FROM gsc_queries").fetchall()
    assert len(stored) == 1
    assert stored[0]["page"] == stored[0]["device"] == stored[0]["country"] == ""
    assert stored[0]["clicks"] == 2


def test_legacy_nullable_gsc_schema_migrates_and_deduplicates(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE gsc_queries (
          property_url TEXT NOT NULL, data_date TEXT NOT NULL, query TEXT NOT NULL,
          page TEXT, device TEXT, country TEXT, search_type TEXT NOT NULL,
          clicks INTEGER NOT NULL, impressions INTEGER NOT NULL, ctr REAL NOT NULL,
          position REAL NOT NULL, pulled_at TEXT NOT NULL
        );
        CREATE INDEX idx_gsc_queries_prop_date ON gsc_queries(property_url, data_date);"""
    )
    conn.executemany(
        "INSERT INTO gsc_queries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(PROPERTY, "2026-07-01", "q", None, None, None, "web", 1, 10, 0.1, 5, "2026-08-01"),
         (PROPERTY, "2026-07-01", "q", None, None, None, "web", 9, 90, 0.1, 5, "2026-08-02")],
    )
    conn.commit()
    conn.close()
    migrated = Database(path)
    row = migrated._conn.execute("SELECT * FROM gsc_queries").fetchone()
    assert migrated._conn.execute("SELECT COUNT(*) FROM gsc_queries").fetchone()[0] == 1
    assert row["clicks"] == 9 and row["page"] == row["device"] == row["country"] == ""
    migrated.close()
    rerun = Database(path)
    assert rerun._conn.execute("SELECT COUNT(*) FROM gsc_queries").fetchone()[0] == 1
    rerun.close()


def test_legacy_gsc_migration_failure_rolls_back_table_and_data(tmp_path):
    path = tmp_path / "legacy-invalid.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE gsc_queries (
          property_url TEXT NOT NULL, data_date TEXT NOT NULL, query TEXT NOT NULL,
          page TEXT, device TEXT, country TEXT, search_type TEXT,
          clicks INTEGER NOT NULL, impressions INTEGER NOT NULL, ctr REAL NOT NULL,
          position REAL NOT NULL, pulled_at TEXT NOT NULL
        );
        CREATE INDEX idx_gsc_queries_prop_date ON gsc_queries(property_url, data_date);"""
    )
    conn.execute(
        "INSERT INTO gsc_queries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (PROPERTY, "2026-07-01", "q", None, None, None, None, 1, 10, 0.1, 5, "2026-08-01"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError):
        Database(path)

    check = sqlite3.connect(path)
    tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    row = check.execute("SELECT * FROM gsc_queries").fetchone()
    assert "gsc_queries" in tables
    assert "gsc_queries_legacy" not in tables
    assert row == (PROPERTY, "2026-07-01", "q", None, None, None, None, 1, 10, 0.1, 5, "2026-08-01")
    check.close()


def test_inspect_unknown_mobile_verdict_is_null(ws, db, connected, stub):
    _GscStub.inspect_payload["inspectionResult"].pop("mobileUsabilityResult", None)
    result = gsc.inspect_url(db, ws, "acme", "https://www.example.com/new-article/")
    assert result["mobile_usable"] is None


def test_import_csv_writes_page_dimension(ws, db, tmp_path):
    csv_text = "Date,Query,Page,Impressions,Clicks,CTR,Position\n2026-07-01,,https://www.example.com/p,80,4,5.0%,3.0\n"
    path = tmp_path / "export.csv"
    path.write_text(csv_text, encoding="utf-8-sig")
    gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    rows = db.gsc_query_rows(PROPERTY, "2026-07-01", "2026-07-01")
    assert rows[0]["page"] == "https://www.example.com/p"
    assert rows[0]["query"] == ""


# ---------------------------------------------------------------------------
# T7 — insights + status
# ---------------------------------------------------------------------------


def _seed_queries(db, ws) -> None:
    db.upsert_gsc_property("acme", PROPERTY, "gcloud-adc", None)
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=27)
    mid = start + timedelta(days=13)
    rows = []
    for day_offset in range(28):
        day = (start + timedelta(days=day_offset)).isoformat()
        first_half = day < mid.isoformat()
        rows.append(
            {
                "property_url": PROPERTY,
                "data_date": day,
                "query": "broad head term",
                "page": "https://www.example.com/a",
                "device": None,
                "country": None,
                "search_type": "web",
                "clicks": 0,
                "impressions": 180,
                "ctr": 0.0,
                "position": 12.0,
                "pulled_at": "2026-08-01T00:00:00",
            }
        )
        rows.append(
            {
                "property_url": PROPERTY,
                "data_date": day,
                "query": "trending phrase",
                "page": "https://www.example.com/b",
                "device": None,
                "country": None,
                "search_type": "web",
                "clicks": 0,
                "impressions": 40 if first_half else 80,
                "ctr": 0.0,
                "position": 8.0,
                "pulled_at": "2026-08-01T00:00:00",
            }
        )
    db.upsert_gsc_query_rows(PROPERTY, rows)


def test_insights_reports(ws, db):
    _seed_queries(db, ws)
    result = gsc.insights(db, ws, "acme", window=28)
    assert result["property"] == PROPERTY
    low_ctr = [r for r in result["high_impressions_low_ctr"] if r["query"] == "broad head term"]
    assert len(low_ctr) == 1
    assert low_ctr[0]["impressions"] == 28 * 180
    assert low_ctr[0]["ctr"] == 0.0
    rising = [r for r in result["rising_queries"] if r["query"] == "trending phrase"]
    assert len(rising) == 1
    assert rising[0]["impressions_first_half"] == 13 * 40
    assert rising[0]["impressions_second_half"] == 15 * 80
    assert rising[0]["growth"] == round((15 * 80) / (13 * 40), 2)
    assert result["partial"] is True
    assert "not a complete keyword database" in result["data_limit_note"]


def test_insights_partial_false_when_window_fully_pulled(ws, db):
    _seed_queries(db, ws)
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=27)
    for dim in ("date,query", "date,page"):
        for offset in range(28):
            db.mark_gsc_pull_complete(PROPERTY, dim, (start + timedelta(days=offset)).isoformat())
    result = gsc.insights(db, ws, "acme", window=28)
    assert result["partial"] is False
    assert result["coverage"] == 1.0


def test_insights_partial_true_when_pull_incomplete(ws, db):
    _seed_queries(db, ws)
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=27)
    for offset in range(10):  # only the query dimension, first 10 days
        db.mark_gsc_pull_complete(PROPERTY, "date,query", (start + timedelta(days=offset)).isoformat())
    result = gsc.insights(db, ws, "acme", window=28)
    assert result["partial"] is True
    assert result["coverage"] == pytest.approx(10 / 56, abs=0.001)


def test_insights_csv_source_coverage_by_dates(ws, db, tmp_path):
    day = (date.today() - timedelta(days=3)).isoformat()
    path = tmp_path / "export.csv"
    path.write_text(
        f"Date,Query,Impressions,Clicks,CTR,Position\n{day},first,100,1,1.0%,4.0\n",
        encoding="utf-8-sig",
    )
    gsc.import_gsc_csv(db, ws, "acme", path, property_url=PROPERTY)
    result = gsc.insights(db, ws, "acme", window=28)
    assert result["source"] == "csv-import"
    assert result["partial"] is True
    assert result["coverage"] == round(1 / 28, 3)


def test_cli_version_is_successful():
    proc = subprocess.run([sys.executable, "-m", "seo_writer", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "seo-writer" in proc.stdout and "rules" in proc.stdout


def test_insights_requires_property(ws, db):
    with pytest.raises(gsc.GscError) as exc:
        gsc.insights(db, ws, "acme")
    assert "no connected GSC property" in str(exc.value)


def test_insights_url_performance_vs_audit_baseline(ws, db, tmp_path):
    _seed_queries(db, ws)
    audit_dir = ws.root / "brands" / "acme" / "site-crawl"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "seo-audit.yaml").write_text("score: 42\nrubric: seo-audit-skill v1\n", encoding="utf-8")
    result = gsc.insights(db, ws, "acme", window=28, url="https://www.example.com/a")
    perf = result["url_performance"]
    assert perf["has_data"] is True
    assert perf["impressions"] == 28 * 180
    assert perf["avg_position"] == 12.0
    assert perf["audit_baseline"] == {"score": 42, "rubric": "seo-audit-skill v1"}


def test_gsc_status_bound_and_synced(ws, db, connected):
    db.upsert_gsc_query_rows(
        PROPERTY,
        [
            {
                "property_url": PROPERTY,
                "data_date": "2026-07-01",
                "query": "q1",
                "page": "https://www.example.com/a",
                "device": None,
                "country": None,
                "search_type": "web",
                "clicks": 1,
                "impressions": 10,
                "ctr": 0.1,
                "position": 5.0,
                "pulled_at": "2026-08-01T00:00:00",
            }
        ],
    )
    db.update_gsc_property_synced("acme", "2026-08-01T10:00:00")
    result = gsc.gsc_status(db, ws, "acme")
    assert result["connected"] is True
    assert result["property"] == PROPERTY
    assert result["auth_path"] == "gcloud-adc"
    assert result["sync"] == {"start_date": "2026-07-01", "end_date": "2026-07-01", "days": 1}
    assert result["last_synced_at"] == "2026-08-01T10:00:00"
    flat = json.dumps(result)
    assert CLIENT_SECRET not in flat
    assert REFRESH_TOKEN not in flat


def test_gsc_status_unconnected_shows_state(ws, db, monkeypatch, tmp_path):
    monkeypatch.setattr(gsc, "GCLOUD_ADC_PATH", tmp_path / "missing.json")
    result = gsc.gsc_status(db, ws, "acme")
    assert result["connected"] is False
    assert result["credentials"]["adc_file_exists"] is False


# ---------------------------------------------------------------------------
# CLI subprocess flow (env-overridden endpoints, offline)
# ---------------------------------------------------------------------------


def _gsc_env(stub, adc_path: Path) -> dict:
    env = os.environ.copy()
    env["SEO_WRITER_GSC_API_BASE"] = stub.url
    env["SEO_WRITER_GSC_TOKEN_URL"] = f"{stub.url}/token"
    env["SEO_WRITER_GSC_TOKENINFO_URL"] = f"{stub.url}/tokeninfo"
    env["SEO_WRITER_GSC_ADC_PATH"] = str(adc_path)
    return env


def test_cli_gsc_flow_end_to_end(tmp_path, stub, adc_file):
    data_dir = tmp_path / "data"
    code, _, err = run_json_cli(["brand", "create", "acme"], data_dir)
    assert code == 0, err

    def cli(*args: str) -> tuple[int, dict | None, str]:
        proc = subprocess.run(
            [
                sys.executable, "-m", "seo_writer", "--data-dir", str(data_dir), "--json",
                "--workspace", "default", *args,
            ],
            capture_output=True,
            text=True,
            env=_gsc_env(stub, adc_file),
        )
        return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else None, proc.stderr

    code, out, err = cli("gsc", "setup", "--brand", "acme")
    assert code == 0, err
    assert out["status"] == "ready"
    assert out["auth_path"] == "gcloud-adc"

    code, out, err = cli("gsc", "sites", "--brand", "acme")
    assert code == 0, err
    assert out["sites"][0]["site_url"] == PROPERTY

    code, out, err = cli("gsc", "connect", "--brand", "acme", "--property", PROPERTY)
    assert code == 0, err
    assert out["status"] == "connected"

    code, out, err = cli(
        "gsc", "pull", "--brand", "acme", "--start-date", "2026-07-01", "--end-date", "2026-07-02"
    )
    assert code == 0, err
    assert out["api_calls"] == 4

    code, out, err = cli("gsc", "status", "--brand", "acme")
    assert code == 0, err
    assert out["connected"] is True
    assert out["sync"]["start_date"] == "2026-07-01"
    assert out["sync"]["end_date"] == "2026-07-02"

    code, out, err = cli("gsc", "insights", "--brand", "acme", "--window", "28")
    assert code == 0, err


def test_cli_gsc_quota_error_is_retryable_json(tmp_path, stub, adc_file):
    data_dir = tmp_path / "data"
    run_json_cli(["brand", "create", "acme"], data_dir)

    def cli(*args: str) -> tuple[int, dict | None, str]:
        proc = subprocess.run(
            [
                sys.executable, "-m", "seo_writer", "--data-dir", str(data_dir), "--json",
                "--workspace", "default", *args,
            ],
            capture_output=True,
            text=True,
            env=_gsc_env(stub, adc_file),
        )
        return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else None, proc.stderr

    code, _, err = cli("gsc", "connect", "--brand", "acme", "--property", PROPERTY)
    assert code == 0, err
    _GscStub.sa_429_remaining = 99
    code, _, stderr = cli(
        "gsc", "pull", "--brand", "acme", "--start-date", "2026-07-01", "--end-date", "2026-07-01"
    )
    assert code == 1
    payload = json.loads(stderr)
    assert payload["error"] == "GscQuotaError"
    assert payload["retryable"] is True
    assert CLIENT_SECRET not in stderr and REFRESH_TOKEN not in stderr


def test_cli_gsc_unknown_brand_fails(tmp_path, stub, adc_file):
    data_dir = tmp_path / "data"
    proc = subprocess.run(
        [
            sys.executable, "-m", "seo_writer", "--data-dir", str(data_dir), "--json",
            "--workspace", "default", "gsc", "status", "--brand", "nope",
        ],
        capture_output=True,
        text=True,
        env=_gsc_env(stub, adc_file),
    )
    assert proc.returncode == 1
