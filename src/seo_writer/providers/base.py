"""Provider protocol and standardized result envelope.

Business code depends only on these interfaces; DataForSEO/Reddit/LLM
implementations are pluggable and never leak secrets. Every result carries
provider, request_fingerprint, timestamp, cost_estimate, retryability and
source_confidence so the business layer can audit and price each call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..ids import utcnow


@dataclass
class ProviderResult:
    provider: str
    operation: str
    data: Any
    profile: str = ""
    request_fingerprint: str = ""
    timestamp: str = field(default_factory=utcnow)
    cost_estimate: float = 0.0
    token_estimate: int | None = None
    source_confidence: str = "mock"
    retryability: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "profile": self.profile,
            "operation": self.operation,
            "request_fingerprint": self.request_fingerprint,
            "timestamp": self.timestamp,
            "cost_estimate": self.cost_estimate,
            "token_estimate": self.token_estimate,
            "source_confidence": self.source_confidence,
            "retryability": self.retryability,
        }


class KeywordProvider(Protocol):
    name: str
    profile: str

    def search_volume(self, keywords: list[str]) -> ProviderResult: ...
    def related(self, keyword: str) -> ProviderResult: ...
    def paa(self, keyword: str) -> ProviderResult: ...


class SerpProvider(Protocol):
    name: str
    profile: str

    def query(self, keyword: str, variant: str) -> ProviderResult: ...


class WebFetchProvider(Protocol):
    name: str
    profile: str

    def fetch(self, url: str) -> ProviderResult: ...


class CommunityProvider(Protocol):
    name: str
    profile: str

    def discover(self, query: str) -> ProviderResult: ...
    def read_thread(self, url: str) -> ProviderResult: ...


class LLMProvider(Protocol):
    name: str
    profile: str

    def generate_outline(
        self, brief: dict[str, Any], evidence: list[dict[str, Any]], claims: list[dict[str, Any]]
    ) -> ProviderResult: ...
    def generate_draft(self, outline: str, claims: list[dict[str, Any]]) -> ProviderResult: ...
    def generate_metadata(self, outline: str, draft: str, paa_pool: list[str]) -> ProviderResult: ...
