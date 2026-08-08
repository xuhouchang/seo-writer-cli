"""Deterministic mock SerpProvider + WebFetchProvider.

The mock executes the three mandatory query variants (exact, how-to,
decision/failure) and "opens" a configurable number of pages, producing
structured query snapshots and opened-page evidence with explicit typing so
the research gate can distinguish them.
"""

from __future__ import annotations

import re

from ..errors import PermanentProviderError, TransientProviderError
from .base import ProviderResult
from .fixtures import fingerprint, load_fixture, pop_failures

SERP_COST = 0.0015
FETCH_COST = 0.001

VARIANT_SUFFIX = {"head": "", "howto": "how to ", "decision": " vs alternatives; why it fails"}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "topic"


class MockSerpProvider:
    name = "mock"
    profile = "mock-serp"

    def __init__(self, profile: str = "mock-serp", fixture_dir: str | None = None) -> None:
        self.profile = profile
        self.fixture = load_fixture(fixture_dir, "serp")

    def _maybe_fail(self, op: str) -> None:
        for kind in pop_failures(self.fixture, op, self.profile):
            if kind == "permanent":
                raise PermanentProviderError(self.name, f"fixture: permanent failure on {op}")
            raise TransientProviderError(self.name, f"fixture: transient failure on {op}")

    def query(self, keyword: str, variant: str) -> ProviderResult:
        """One query snapshot: structured SERP observation (never an opened page)."""
        self._maybe_fail("query")
        query = f"{VARIANT_SUFFIX[variant]}{keyword}"
        base = _slugify(keyword)
        organic = [
            {
                "rank": i,
                "url": f"https://www.example.com/{base}-{i}",
                "title": f"{query.title()} guide {i}",
                "type": "organic",
            }
            for i in range(1, 11)
        ]
        data = {
            "query": query,
            "variant": variant,
            "aio_visible": bool(self.fixture.get("aio_visible", False)),
            "aio_conclusion": self.fixture.get("aio_conclusion", "n/a")
            if not self.fixture.get("aio_visible")
            else "generic summary",
            "paa": self.fixture.get("paa")
            or [f"What is the right way to {query}?", f"Does {query} actually work?"],
            "related_searches": self.fixture.get("related") or [f"{query} workflow", f"{query} tools"],
            "organic": organic,
        }
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="serp.query",
            data=data,
            request_fingerprint=fingerprint("query", keyword, variant, self.profile),
            cost_estimate=SERP_COST,
            source_confidence="mock_structured",
        )


class MockWebFetchProvider:
    name = "mock"
    profile = "mock-webfetch"

    def __init__(self, profile: str = "mock-webfetch", fixture_dir: str | None = None) -> None:
        self.profile = profile
        self.fixture = load_fixture(fixture_dir, "webfetch")

    def _maybe_fail(self, op: str) -> None:
        for kind in pop_failures(self.fixture, op, self.profile):
            if kind == "permanent":
                raise PermanentProviderError(self.name, f"fixture: permanent failure on {op}")
            raise TransientProviderError(self.name, f"fixture: transient failure on {op}")

    def fetch(self, url: str) -> ProviderResult:
        """Opens a page body: this is what 'opened in current run' means."""
        self._maybe_fail("fetch")
        slug = url.rstrip("/").split("/")[-1]
        data = {
            "url": url,
            "title": f"Page for {slug}",
            "headings": [f"## {slug} overview", "## Workflow steps", "## Limitations"],
            "main_claims": [f"{slug} describes its workflow", f"{slug} covers common failure modes"],
            "limitations": ["coverage varies by use case", "vendor claims are not independently verified"],
            "content_type": "editorial guide",
            "fetch_method": "mock_webfetch",
        }
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation="webfetch.fetch",
            data=data,
            request_fingerprint=fingerprint("fetch", url, self.profile),
            cost_estimate=FETCH_COST,
            source_confidence="mock",
        )
