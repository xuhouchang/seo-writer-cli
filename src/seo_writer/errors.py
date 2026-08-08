"""Error taxonomy for seo-writer.

Exit-code contract:
  0  success
  1  business/validation failure (gate, approval, claim safety, provider)
  2  usage error (bad flags, missing workspace, unknown id)
"""

from __future__ import annotations


class SeoWriterError(Exception):
    exit_code = 1


class UsageError(SeoWriterError):
    exit_code = 2


class NotFoundError(SeoWriterError):
    pass


class StateTransitionError(SeoWriterError):
    pass


class GateNotPassedError(SeoWriterError):
    pass


class ApprovalRequiredError(SeoWriterError):
    pass


class ApprovalInvalidatedError(SeoWriterError):
    pass


class ValidationFailedError(SeoWriterError):
    """Deterministic validation failed; reasons are auditable and user-facing."""

    def __init__(self, reasons: list[str], step: str = "validate") -> None:
        self.reasons = list(reasons)
        self.step = step
        super().__init__("; ".join(reasons) or "validation failed")


class ProviderError(SeoWriterError):
    def __init__(self, provider: str, message: str, retryable: bool = False) -> None:
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"[{provider}] {message}")


class TransientProviderError(ProviderError):
    """Temporary failure (timeout, 5xx, rate burst). May be retried per policy."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(provider, message, retryable=True)


class PermanentProviderError(ProviderError):
    """Auth, quota, compliance or data-integrity failure. Never silently retried."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(provider, message, retryable=False)


class IdempotencyError(SeoWriterError):
    pass
