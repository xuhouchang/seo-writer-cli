"""Shared constants for the Google Search Console integration.

Everything here is plain data — endpoint URLs, scopes, limits and label
maps — so it can be imported by every ``seo_writer.gsc`` submodule without
creating import cycles.
"""

from __future__ import annotations

import os
from pathlib import Path

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GCLOUD_SCOPES = [CLOUD_PLATFORM_SCOPE, SCOPE]
GCLOUD_SCOPE_ARG = ",".join(GCLOUD_SCOPES)
GCLOUD_ADC_PATH = Path(
    os.environ.get("SEO_WRITER_GSC_ADC_PATH")
    or Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
)
TOKEN_URL = os.environ.get("SEO_WRITER_GSC_TOKEN_URL") or "https://oauth2.googleapis.com/token"
TOKENINFO_URL = os.environ.get("SEO_WRITER_GSC_TOKENINFO_URL") or "https://oauth2.googleapis.com/tokeninfo"
AUTH_URL = os.environ.get("SEO_WRITER_GSC_AUTH_URL") or "https://accounts.google.com/o/oauth2/v2/auth"
API_BASE = os.environ.get("SEO_WRITER_GSC_API_BASE") or "https://searchconsole.googleapis.com"
GCLOUD_BIN = "gcloud"
ROW_LIMIT = 25_000
DEFAULT_PULL_DAYS = 30
FRESHNESS_DELAY_DAYS = 3
AUTH_TIMEOUT_S = 300.0
MAX_PULL_DAYS = 500  # covers the 16-month API window plus slack
MAX_AUTH_REFRESH_RETRIES = 3  # consecutive 401s tolerated per page before giving up

COVERAGE_LABELS = {
    "SUBMITTED_AND_INDEXED": "Submitted and indexed",
    "SUBMITTED_BUT_NOT_INDEXED": "Submitted, not indexed",
    "CRAWLED_AND_CURRENTLY_NOT_INDEXED": "Crawled, currently not indexed",
    "DUPLICATE_GOOGLE_CHOSEN_CANONICAL": "Duplicate: Google chose a different canonical",
    "DUPLICATE_USER_CHOSEN_CANONICAL": "Duplicate: submitted URL is not the canonical",
    "PAGE_WITH_REDIRECT": "Page with redirect",
    "PAGE_WITH_SOFT_404": "Page with soft 404",
    "NOT_FOUND": "Not found",
    "NOT_CRAWLABLE": "Not crawlable",
    "CRAWL_ANOMALY": "Crawl anomaly",
    "SERVER_ERROR": "Server error",
    "UNSPECIFIED": "Unspecified",
}
