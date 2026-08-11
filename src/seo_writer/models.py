"""Pydantic schemas for all customer configuration files.

No default here is customer-specific: the generic example brand pack ships
under examples/ and is never implicitly loaded. Provider secrets never appear
in these files; only profile references (keys come from env/secret store).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SAFETY_LEVELS = {"safe", "qualified", "topic_specific", "blocked"}
FETCH_METHODS = {
    "chrome",
    "jina",
    "web_fetch",
    "reddit_api",
    "mock_webfetch",
    "mock_reddit",
    "mock_api",
    "snippet_only",
}
EVIDENCE_ORIGINS = {"current_run", "structured_discovery", "reused_prior_run_evidence"}


class BrandYaml(BaseModel):
    """brand.yaml — who the customer is writing for."""

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    market: str = "US"
    language: str = "en"
    audience: str = ""
    tone: str = "analytical, practical"
    competitors: list[str] = []


class ClaimRule(BaseModel):
    """One row of the brand fact ledger (generalizes the Skill's claim subset)."""

    claim_id: str = Field(pattern=r"^[A-Z0-9-]+$")
    claim: str
    safety_level: Literal["safe", "qualified", "topic_specific", "blocked"]
    allowed_wording: str = ""
    disallowed_wording: list[str] = Field(default_factory=list)
    source_url: str = ""
    evidence_date: str = ""
    evidence_mode: str = ""
    volatility: str = "medium"
    guardrail: str = ""
    reason: str = ""
    decision: str = "approved"

    @field_validator("decision")
    @classmethod
    def _decision_consistent(cls, value: str, info) -> str:
        level = info.data.get("safety_level")
        if level == "blocked":
            if value not in {"blocked", "not_approved"}:
                raise ValueError("blocked claims must carry decision 'blocked' or 'not_approved'")
        elif value != "approved":
            raise ValueError("usable claims must carry decision 'approved'")
        return value


class MaterialTerm(BaseModel):
    """A substantive claim trigger: if the term appears in copy, its claim_id
    must be approved in the run's fact ledger (generalizes SPK-xx mapping)."""

    term: str
    claim_id: str


class FactsYaml(BaseModel):
    """facts.yaml — the per-brand claim ledger."""

    brand: str
    facts_version: int = 1
    rules: list[ClaimRule] = Field(min_length=1)
    material_terms: list[MaterialTerm] = Field(default_factory=list)

    @field_validator("rules")
    @classmethod
    def _unique_claim_ids(cls, rules: list[ClaimRule]) -> list[ClaimRule]:
        ids = [r.claim_id for r in rules]
        if len(set(ids)) != len(ids):
            raise ValueError("claim_ids must be unique within a fact pack")
        return rules


class ResearchGatePolicy(BaseModel):
    """Thresholds. Defaults are the Skill's gate and may not be weakened in
    shipped policies below these values (enforced at load time)."""

    min_queries: int = 3
    min_opened_serp_pages: int = 5
    min_opened_threads: int = 10
    min_subreddits: int = 4
    require_second_platform: bool = True


SKILL_GATE_FLOOR = ResearchGatePolicy()


class ProviderPolicy(BaseModel):
    name: str = "mock"
    profile: str = "mock-default"
    fixture_dir: str | None = None
    base_url: str | None = None
    location_code: int = 2840
    language_code: str = "en"
    device: str = "desktop"
    retryable: bool = True
    budget_cap: float | None = None


class PolicyYaml(BaseModel):
    """policy.yaml — deterministic run rules; never holds secrets."""

    brand: str
    providers: dict[str, ProviderPolicy] = Field(default_factory=dict)
    research_gate: ResearchGatePolicy = Field(default_factory=ResearchGatePolicy)
    retries: int = 2
    cost_limit_per_run: float = 5.0

    @field_validator("research_gate")
    @classmethod
    def _gate_not_weaker_than_skill(cls, gate: ResearchGatePolicy) -> ResearchGatePolicy:
        for field, floor in (
            ("min_queries", SKILL_GATE_FLOOR.min_queries),
            ("min_opened_serp_pages", SKILL_GATE_FLOOR.min_opened_serp_pages),
            ("min_opened_threads", SKILL_GATE_FLOOR.min_opened_threads),
            ("min_subreddits", SKILL_GATE_FLOOR.min_subreddits),
        ):
            if getattr(gate, field) < floor:
                raise ValueError(f"research_gate.{field} may not be lower than the Skill floor ({floor})")
        return gate


class TopicYaml(BaseModel):
    """topic.yaml — one ArticleRun's brief."""

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str
    seed_keywords: list[str] = Field(min_length=1)
    intent: Literal["TOFU", "MOFU", "BOFU"] = "MOFU"
    target_word_count: int = 1500
    audience: str = ""
    existing_blog_urls: list[str] = Field(default_factory=list)
    notes: str = ""
