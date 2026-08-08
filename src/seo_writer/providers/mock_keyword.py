"""Deterministic mock KeywordProvider (offline; no paid API involved)."""

from __future__ import annotations

from ..errors import PermanentProviderError, TransientProviderError
from .base import ProviderResult
from .fixtures import fingerprint, load_fixture, pop_failures

KW_COST = 0.0004  # USD per call (mock)
BUCKETS = ("head", "platform", "method", "failure", "workflow")


class MockKeywordProvider:
    name = "mock"
    profile = "mock-default"

    def __init__(self, profile: str = "mock-default", fixture_dir: str | None = None) -> None:
        self.profile = profile
        self.fixture = load_fixture(fixture_dir, "keyword")

    def _maybe_fail(self, op: str) -> None:
        for kind in pop_failures(self.fixture, op, self.profile):
            if kind == "permanent":
                raise PermanentProviderError(self.name, f"fixture: permanent failure on {op}")
            raise TransientProviderError(self.name, f"fixture: transient failure on {op}")

    def search_volume(self, keywords: list[str]) -> ProviderResult:
        self._maybe_fail("search_volume")
        data = {
            kw: {
                "volume": self.fixture.get("volume", 3200 + len(kw)),
                "difficulty": self.fixture.get("difficulty", 42),
            }
            for kw in keywords
        }
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="keyword.search_volume",
            data=data,
            request_fingerprint=fingerprint("search_volume", keywords, self.profile),
            cost_estimate=KW_COST,
            source_confidence="mock",
        )

    def related(self, keyword: str) -> ProviderResult:
        self._maybe_fail("related")
        data = self.fixture.get("related") or [
            f"{keyword} workflow",
            f"{keyword} vs alternatives",
            f"{keyword} for beginners",
            f"why {keyword} fails",
            f"{keyword} step by step",
        ]
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="keyword.related",
            data=data,
            request_fingerprint=fingerprint("related", keyword, self.profile),
            cost_estimate=KW_COST,
            source_confidence="mock",
        )

    def paa(self, keyword: str) -> ProviderResult:
        self._maybe_fail("paa")
        data = self.fixture.get("paa") or [
            f"How do you {keyword}?",
            f"What tools help with {keyword}?",
            f"When does {keyword} not work?",
            f"How long does {keyword} take?",
        ]
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="keyword.paa",
            data=data,
            request_fingerprint=fingerprint("paa", keyword, self.profile),
            cost_estimate=KW_COST,
            source_confidence="mock",
        )
