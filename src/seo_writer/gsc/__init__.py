"""Google Search Console integration: credentials, OAuth, pull, insights.

Closes the loop production → publish → measure → iterate. Everything uses
only the standard library (urllib / hashlib / base64 / http.server / csv /
sqlite3) — no google-api-python-client, no requests, no google-auth-oauthlib.

Credentials and data stay on the customer's machine: the gcloud ADC file is
read in place, a self-built client writes a chmod-600 client json + token
file under the workspace. Secrets never enter git, logs, or error messages —
audit events only carry property references and file paths.

Tests run against synthetic credentials and local stub endpoints; real
customer data never appears in fixtures. Endpoint URLs can be redirected
through SEO_WRITER_GSC_* environment variables (used by the CLI test
harness); functions also accept explicit URLs for direct unit tests.

The implementation is split into submodules (see the ``gsc/`` package):
``_constants``, ``_errors``, ``_auth`` (credentials), ``_http`` (transport),
``_backoff``, ``_pull``, ``_csv``, ``_insights`` and ``_oauth``. This module
re-exports the full public API so ``from seo_writer import gsc`` and
``from seo_writer.gsc import X`` both keep working unchanged.
"""

from __future__ import annotations

from . import (
    _auth,
    _backoff,
    _constants,
    _csv,
    _errors,
    _http,
    _insights,
    _oauth,
    _pull,
)
from ._auth import (
    Credentials,
    client_json_path,
    gsc_brand_dir,
    import_client_json,
    load_adc,
    load_client_json,
    load_credentials,
    load_token,
    save_token,
    token_file_path,
)
from ._backoff import RateLimiter, with_backoff
from ._constants import (
    API_BASE,
    AUTH_TIMEOUT_S,
    AUTH_URL,
    CLOUD_PLATFORM_SCOPE,
    COVERAGE_LABELS,
    DEFAULT_PULL_DAYS,
    FRESHNESS_DELAY_DAYS,
    GCLOUD_ADC_PATH,
    GCLOUD_BIN,
    GCLOUD_SCOPE_ARG,
    GCLOUD_SCOPES,
    MAX_AUTH_REFRESH_RETRIES,
    MAX_PULL_DAYS,
    ROW_LIMIT,
    SCOPE,
    TOKEN_URL,
    TOKENINFO_URL,
)
from ._csv import import_gsc_csv
from ._errors import GscAuthError, GscError, GscQuotaError, GscTransientError
from ._http import check_scope, refresh_access_token, verify_credentials
from ._insights import gsc_status, insights
from ._oauth import (
    build_auth_url,
    desktop_auth,
    exchange_code,
    gcloud_auth,
    pkce_pair,
    receive_code_via_loopback,
    run_auth,
    setup_guide,
)
from ._pull import (
    connect_property,
    default_pull_window,
    inspect_url,
    list_sites,
    pull_search_analytics,
)

__all__ = [
    # constants
    "API_BASE",
    "AUTH_TIMEOUT_S",
    "AUTH_URL",
    "CLOUD_PLATFORM_SCOPE",
    "COVERAGE_LABELS",
    "DEFAULT_PULL_DAYS",
    "FRESHNESS_DELAY_DAYS",
    "GCLOUD_ADC_PATH",
    "GCLOUD_BIN",
    "GCLOUD_SCOPE_ARG",
    "GCLOUD_SCOPES",
    "MAX_AUTH_REFRESH_RETRIES",
    "MAX_PULL_DAYS",
    "ROW_LIMIT",
    "SCOPE",
    "TOKENINFO_URL",
    "TOKEN_URL",
    # errors
    "GscAuthError",
    "GscError",
    "GscQuotaError",
    "GscTransientError",
    # credentials
    "Credentials",
    "client_json_path",
    "gsc_brand_dir",
    "import_client_json",
    "load_adc",
    "load_client_json",
    "load_credentials",
    "load_token",
    "save_token",
    "token_file_path",
    # transport / backoff
    "RateLimiter",
    "check_scope",
    "refresh_access_token",
    "verify_credentials",
    "with_backoff",
    # pull / inspect / csv / insights
    "connect_property",
    "default_pull_window",
    "gsc_status",
    "import_gsc_csv",
    "insights",
    "inspect_url",
    "list_sites",
    "pull_search_analytics",
    # auth flows
    "build_auth_url",
    "desktop_auth",
    "exchange_code",
    "gcloud_auth",
    "pkce_pair",
    "receive_code_via_loopback",
    "run_auth",
    "setup_guide",
]


def __getattr__(name: str):
    """Resolve names this module does not bind (PEP 562).

    ``monkeypatch.setattr(gsc, "X", value)`` — the test-suite's contract —
    writes into this module's ``__dict__``; the submodules read ``gsc.X`` at
    call time so a patch is always observed. Anything not re-exported above
    (e.g. the private ``_http_request`` transport helper) is forwarded to the
    submodule that owns it, keeping ``gsc.<name>`` resolution identical to the
    pre-split single ``gsc.py`` module.
    """
    for mod in (_constants, _auth, _http, _pull, _oauth, _insights, _csv, _backoff, _errors):
        try:
            return getattr(mod, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
