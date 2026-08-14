"""Exponential backoff and request rate limiting for GSC API calls."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ._errors import GscError, GscQuotaError, GscTransientError


def _jittered_delay(attempt: int, base: float, max_delay: float) -> float:
    delay = min(max_delay, base * (2**attempt))
    # deterministic pseudo-jitter (keeps tests reproducible)
    wobble = 0.9 + 0.2 * ((attempt * 7919) % 10) / 10
    return round(delay * wobble, 3)


def with_backoff(
    fn: Callable[..., Any],
    *args: Any,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """Run fn with exponential backoff on quota/transient errors (1s→60s + jitter)."""
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (GscQuotaError, GscTransientError):
            if attempt == attempts - 1:
                raise
            sleep(_jittered_delay(attempt, base_delay, max_delay))
    raise GscError("backoff loop exhausted without a result")  # unreachable


class RateLimiter:
    """Pace requests to at most one per min_interval (defensive 1,200 QPM cap)."""

    def __init__(
        self,
        min_interval: float = 60.0 / 1200.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_interval = min_interval
        self._sleep = sleep
        self._now = now
        self._last: float = 0.0

    def wait(self) -> None:
        elapsed = self._now() - self._last
        if self._last and elapsed < self._min_interval:
            self._sleep(self._min_interval - elapsed)
        self._last = self._now()
