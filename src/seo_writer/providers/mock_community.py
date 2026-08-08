"""Deterministic mock CommunityProvider.

Discovers candidate discussions (structured_discovery — never counted as
opened) and reads threads (opened in current run). Subreddit spread, thread
count and the second-platform record are fixture-controllable so the research
gate has real failure paths.
"""

from __future__ import annotations

import re

from ..errors import PermanentProviderError, TransientProviderError
from .base import ProviderResult
from .fixtures import fingerprint, load_fixture, pop_failures

DISCOVER_COST = 0.0005
THREAD_COST = 0.002

DEFAULT_SUBREDDITS = ("r/NewTubers", "r/YouTubeCreators", "r/ContentCreators", "r/VideoEditing")


class MockCommunityProvider:
    name = "mock"
    profile = "mock-community"

    def __init__(self, profile: str = "mock-community", fixture_dir: str | None = None) -> None:
        self.profile = profile
        self.fixture = load_fixture(fixture_dir, "community")

    def _maybe_fail(self, op: str) -> None:
        for kind in pop_failures(self.fixture, op, self.profile):
            if kind == "permanent":
                raise PermanentProviderError(self.name, f"fixture: permanent failure on {op}")
            raise TransientProviderError(self.name, f"fixture: transient failure on {op}")

    def _thread_url(self, sub: str, n: int) -> str:
        subname = sub.replace("r/", "")
        return f"https://www.reddit.com/r/{subname}/comments/{subname[:6]}{n:03d}/thread-{n}"

    def discover(self, query: str) -> ProviderResult:
        """Discovery candidates only — snippet-level, never counted as opened."""
        self._maybe_fail("discover")
        n_candidates = int(self.fixture.get("candidate_threads", 12))
        subreddits = self.fixture.get("subreddits") or list(DEFAULT_SUBREDDITS)
        data = [
            {
                "url": self._thread_url(subreddits[i % len(subreddits)], i + 1),
                "platform": f"Reddit / {subreddits[i % len(subreddits)]}",
                "title": f"Struggling with {query} (candidate {i + 1})",
                "snippet_only": True,
            }
            for i in range(n_candidates)
        ]
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="community.discover",
            data=data,
            request_fingerprint=fingerprint("discover", query, self.profile),
            cost_estimate=DISCOVER_COST,
            source_confidence="mock_discovery",
        )

    def read_thread(self, url: str, platform: str) -> ProviderResult:
        """Opens one thread body: only these count as current-run opened."""
        self._maybe_fail("read_thread")
        sub = re.search(r"r/[A-Za-z0-9_]+", platform)
        label = sub.group(0) if sub else platform
        data = {
            "url": url,
            "platform": platform,
            "title": f"Thread on {label}",
            "original_post_summary": f"Operator describes friction with {label} workflows",
            "high_signal_comments_read": 3,
            "counterexample": "one commenter reports a different workflow with no comparable issue",
            "promotion_disclosure": "none",
            "grade": self.fixture.get("thread_grade", "high"),
            "fetch_method": "mock_reddit",
        }
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="community.read_thread",
            data=data,
            request_fingerprint=fingerprint("read_thread", url, self.profile),
            cost_estimate=THREAD_COST,
            source_confidence="mock",
        )

    def second_platform_thread(self) -> ProviderResult:
        """One opened thread from a non-Reddit platform (second-platform evidence)."""
        url = "https://www.quora.com/What-is-the-hardest-part-of-this-workflow"
        return self.read_thread(url, "Quora / quora.com")
