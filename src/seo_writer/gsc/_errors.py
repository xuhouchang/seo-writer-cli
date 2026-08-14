"""Error taxonomy for the Google Search Console integration."""

from __future__ import annotations

from ..errors import SeoWriterError


class GscError(SeoWriterError):
    """GSC integration failure; retryable errors are safe to back off on."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


class GscAuthError(GscError):
    """Permanent auth failure: revoked token, bad client, missing scope."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class GscQuotaError(GscError):
    """Quota-limited (429 / quota 403); retryable with backoff."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class GscTransientError(GscError):
    """Network / 5xx failure; retryable with backoff."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)
