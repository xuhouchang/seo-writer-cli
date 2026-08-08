"""Provider factory: policy -> provider instances.

Only profile references live in policy.yaml; secrets come from env/secret
stores and are never persisted by the CLI. Phase 1 supports the deterministic
mock profile; real providers resolve to safe stubs that fail permanently.
"""

from __future__ import annotations

from ..errors import UsageError
from ..models import PolicyYaml, ProviderPolicy
from .mock_community import MockCommunityProvider
from .mock_keyword import MockKeywordProvider
from .mock_llm import MockLLMProvider
from .mock_serp import MockSerpProvider, MockWebFetchProvider
from .stubs import DataForSEOProvider, OpenAICompatLLMProvider, RedditProvider

ROLES = ("keyword", "serp", "webfetch", "community", "llm")

MOCK_TYPES = {
    "keyword": MockKeywordProvider,
    "serp": MockSerpProvider,
    "webfetch": MockWebFetchProvider,
    "community": MockCommunityProvider,
    "llm": MockLLMProvider,
}

STUB_TYPES = {
    "keyword": DataForSEOProvider,
    "serp": DataForSEOProvider,
    "community": RedditProvider,
    "llm": OpenAICompatLLMProvider,
    "webfetch": None,  # web fetch has no Phase-1 stub contract; mock only
}


def _policy_for(policy: PolicyYaml, role: str) -> ProviderPolicy:
    return policy.providers.get(role) or ProviderPolicy(name="mock", profile=f"mock-{role}")


def build_providers(policy: PolicyYaml) -> dict[str, object]:
    providers: dict[str, object] = {}
    for role in ROLES:
        p = _policy_for(policy, role)
        if p.name == "mock":
            providers[role] = MOCK_TYPES[role](profile=p.profile, fixture_dir=p.fixture_dir)
            continue
        if role == "webfetch":
            raise UsageError("policy.providers.webfetch: only 'mock' is available in Phase 1")
        if p.name in {"dataforseo", "reddit", "llm"}:
            providers[role] = STUB_TYPES[role](profile=p.profile)
            continue
        raise UsageError(f"policy.providers.{role}: unknown provider name '{p.name}'")
    return providers


def llm_provider(providers: dict[str, object]) -> MockLLMProvider:
    return providers["llm"]  # type: ignore[return-value]
