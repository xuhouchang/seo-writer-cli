"""Provider factory: policy -> provider instances.

Policy selects mock providers for tests/demo fixtures or user-configured real
providers for production. Missing real credentials fail before research and
never fall back to mock data.
"""

from __future__ import annotations

from ..errors import UsageError
from ..models import PolicyYaml, ProviderPolicy
from .mock_community import MockCommunityProvider
from .mock_keyword import MockKeywordProvider
from .mock_llm import MockLLMProvider
from .mock_serp import MockSerpProvider, MockWebFetchProvider
from .real import DataForSEOProvider as RealDataForSEOProvider
from .real import HttpWebFetchProvider
from .real import RedditProvider as RealRedditProvider
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

REAL_TYPES = {
    "keyword": RealDataForSEOProvider,
    "serp": RealDataForSEOProvider,
    "webfetch": HttpWebFetchProvider,
    "community": RealRedditProvider,
}


def _policy_for(policy: PolicyYaml, role: str) -> ProviderPolicy:
    return policy.providers.get(role) or ProviderPolicy(name="mock", profile=f"mock-{role}")


def build_providers(policy: PolicyYaml, data_dir=None) -> dict[str, object]:
    providers: dict[str, object] = {}
    for role in ROLES:
        p = _policy_for(policy, role)
        if p.name == "mock":
            providers[role] = MOCK_TYPES[role](profile=p.profile, fixture_dir=p.fixture_dir)
            continue
        if p.name == "dataforseo" and role in {"keyword", "serp"}:
            if data_dir is None:
                raise UsageError("real providers require the CLI data directory for credentials")
            providers[role] = REAL_TYPES[role](
                profile=p.profile,
                data_dir=data_dir,
                base_url=p.base_url or "https://api.dataforseo.com",
                location_code=p.location_code,
                language_code=p.language_code,
                device=p.device,
            )
            continue
        if p.name == "reddit" and role == "community":
            if data_dir is None:
                raise UsageError("real providers require the CLI data directory for credentials")
            providers[role] = REAL_TYPES[role](
                profile=p.profile,
                data_dir=data_dir,
                oauth_base_url=p.base_url or "https://oauth.reddit.com",
            )
            continue
        if p.name in {"http", "real-http"} and role == "webfetch":
            providers[role] = HttpWebFetchProvider(profile=p.profile)
            continue
        if p.name in {"dataforseo", "reddit", "llm"}:
            if STUB_TYPES[role] is None:
                raise UsageError(
                    f"policy.providers.{role}: provider '{p.name}' is not supported for this role"
                )
            providers[role] = STUB_TYPES[role](profile=p.profile)
            continue
        raise UsageError(f"policy.providers.{role}: unknown provider name '{p.name}'")
    return providers


def llm_provider(providers: dict[str, object]) -> MockLLMProvider:
    return providers["llm"]  # type: ignore[return-value]
