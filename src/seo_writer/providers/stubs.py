"""Safe placeholder entries for real providers (Phase 1).

Interfaces and config schema exist; calling a real provider raises a permanent,
non-retryable error so nothing silently degrades to success. No keys are read,
stored or echoed anywhere. Real implementations land in Phase 2 behind the
same interfaces.
"""

from __future__ import annotations

from ..errors import PermanentProviderError
from .base import ProviderResult

NOT_ENABLED = (
    "provider '{}' (profile '{}') is a Phase-1 placeholder: no real provider call is made offline."
    " Configure a mock profile for offline runs, or wait for the Phase-2 real-provider checklist."
)


class _StubMixin:
    name = "stub"

    def __init__(self, profile: str) -> None:
        self.profile = profile

    def _raise(self, op: str) -> ProviderResult:
        raise PermanentProviderError(self.name, NOT_ENABLED.format(self.name, self.profile) + f" (op={op})")


class DataForSEOProvider(_StubMixin):
    """KeywordProvider + SerpProvider placeholder (DataForSEO)."""

    name = "dataforseo"

    def search_volume(self, keywords) -> ProviderResult:
        return self._raise("keyword.search_volume")

    def related(self, keyword) -> ProviderResult:
        return self._raise("keyword.related")

    def paa(self, keyword) -> ProviderResult:
        return self._raise("keyword.paa")

    def query(self, keyword, variant) -> ProviderResult:
        return self._raise("serp.query")


class RedditProvider(_StubMixin):
    """CommunityProvider placeholder (Reddit OAuth)."""

    name = "reddit"

    def discover(self, query) -> ProviderResult:
        return self._raise("community.discover")

    def read_thread(self, url, platform) -> ProviderResult:
        return self._raise("community.read_thread")


class OpenAICompatLLMProvider(_StubMixin):
    """LLMProvider placeholder for any OpenAI-compatible endpoint (BYOK)."""

    name = "llm"

    def generate_outline(self, brief, evidence, claims) -> ProviderResult:
        return self._raise("llm.outline")

    def generate_draft(self, outline, claims) -> ProviderResult:
        return self._raise("llm.draft")

    def generate_metadata(self, outline, draft, paa_pool) -> ProviderResult:
        return self._raise("llm.metadata")
