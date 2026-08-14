"""Pydantic schemas for all customer configuration files.

No default here is customer-specific: the generic example brand pack ships
under examples/ and is never implicitly loaded. Provider secrets never appear
in these files; only profile references (keys come from env/secret store).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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
    primary_job_to_be_done: str = ""
    buyer_questions: list[str] = Field(default_factory=list)
    candidate_competitors: list[str] = Field(default_factory=list)
    desired_formats: list[str] = Field(default_factory=list)
    gap_hypothesis: str = ""


class BrandProfileReview(BaseModel):
    """English-first factual onboarding payload. No behavioural fields live here."""

    schema_version: int = 1
    company_name: str
    website: str
    target_audience: str = "Not stated"
    primary_use_case: str = "Not stated"
    features: list[str] = Field(default_factory=list)
    advantages: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    limitations_and_non_capabilities: list[str] = Field(default_factory=list)
    competitor_candidates_and_alternatives: list[str] = Field(default_factory=list)
    primary_market: str = "US"
    content_language: Literal["en"] = "en"
    factual_followups: list[str] = Field(default_factory=list)


class CompetitorCandidate(BaseModel):
    name: str
    url: str = ""
    competitor_types: list[
        Literal[
            "business_competitor",
            "search_competitor",
            "ai_cited_competitor",
            "substitute",
            "publisher",
        ]
    ] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class CoverageScore(BaseModel):
    concept_explanation: int = Field(default=0, ge=0, le=3)
    decision_criteria: int = Field(default=0, ge=0, le=3)
    implementation_detail: int = Field(default=0, ge=0, le=3)
    limitations_tradeoffs: int = Field(default=0, ge=0, le=3)
    first_party_evidence: int = Field(default=0, ge=0, le=3)
    evidence_refs: list[str] = Field(default_factory=list)
    summary: str = ""

    @model_validator(mode="after")
    def evidence_for_observed_coverage(self) -> CoverageScore:
        scores = (
            self.concept_explanation,
            self.decision_criteria,
            self.implementation_detail,
            self.limitations_tradeoffs,
            self.first_party_evidence,
        )
        if any(score > 0 for score in scores) and (not self.evidence_refs or not self.summary.strip()):
            raise ValueError("nonzero coverage requires evidence_refs and a summary")
        return self


class ContentMapTopic(BaseModel):
    topic_id: str = Field(pattern=r"^TOPIC-[A-Z0-9-]+$")
    label: str


class ContentMapPage(BaseModel):
    evidence_id: str
    url: str
    domain: str
    competitor_types: list[str] = Field(default_factory=list)
    topic_ids: list[str] = Field(default_factory=list)
    intent_stage: str = "unknown"
    content_format: str = "unknown"
    coverage: CoverageScore = Field(default_factory=CoverageScore)
    observed_claims: list[str] = Field(default_factory=list)
    observed_limitations: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class SampleScope(BaseModel):
    query_count: int = Field(ge=0)
    domain_count: int = Field(ge=0)
    opened_page_count: int = Field(ge=0)


class GapHypothesis(BaseModel):
    gap_id: str
    gap_type: Literal["topic", "intent", "format", "depth"]
    statement: str
    buyer_evidence_refs: list[str] = Field(min_length=1)
    competitor_evidence_refs: list[str] = Field(min_length=1)
    reason_codes: list[str] = Field(default_factory=list)


class EvidenceStatus(BaseModel):
    status: Literal["confirmed", "customer_input_needed", "editorial_hypothesis", "unknown"]
    evidence_refs: list[str] = Field(default_factory=list)


class MarketGap(BaseModel):
    confidence: Literal["weak", "moderate", "strong", "unknown"] = "unknown"
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class BrandFit(BaseModel):
    level: Literal["weak", "moderate", "strong", "unknown"] = "unknown"
    fab_refs: list[str] = Field(default_factory=list)


class DifferentiationReadiness(BaseModel):
    status: Literal["confirmed", "customer_input_needed", "editorial_hypothesis", "unknown"] = "unknown"
    available_case_refs: list[str] = Field(default_factory=list)
    followup_questions: list[str] = Field(default_factory=list)


class OpportunityCard(BaseModel):
    opportunity_id: str
    title: str
    gap_types: list[Literal["topic", "intent", "format", "depth"]] = Field(min_length=1)
    buyer_need: EvidenceStatus
    market_gap: MarketGap
    brand_fit: BrandFit
    differentiation_readiness: DifferentiationReadiness
    recommended_format: str = ""
    risks: list[str] = Field(default_factory=list)
    decision: Literal["candidate", "prioritize", "validate", "reframe", "defer", "excluded"] = "candidate"


class ContentMap(BaseModel):
    schema_version: int = 1
    run_id: str
    sample_scope: SampleScope
    topics: list[ContentMapTopic] = Field(default_factory=list)
    pages: list[ContentMapPage] = Field(default_factory=list)
    gaps: list[GapHypothesis] = Field(default_factory=list)
    opportunities: list[OpportunityCard] = Field(default_factory=list)


class ContextualPrompt(BaseModel):
    prompt_id: str
    section_id: str | None = None
    viewpoint_id: str | None = None
    prompt: str
    response_type: Literal["case", "experience", "decision_rule", "not_a_fit", "proof_request"]

    @model_validator(mode="after")
    def bound_to_outline(self) -> ContextualPrompt:
        if not self.section_id and not self.viewpoint_id:
            raise ValueError("contextual prompt requires section_id or viewpoint_id")
        return self


class CaseLead(BaseModel):
    section_id: str
    viewpoint_id: str
    summary: str
    publishability: Literal["unknown", "public", "confidential"] = "unknown"


class ExperienceLead(BaseModel):
    section_id: str
    viewpoint_id: str
    summary: str
    publishability: Literal["unknown", "public", "confidential"] = "unknown"


class ViewpointCard(BaseModel):
    viewpoint_id: str
    section_id: str
    statement: str
    viewpoint_type: Literal[
        "consensus", "contrarian_hypothesis", "decision_rule", "not_a_fit", "case_prompt", "proof_request"
    ] = "consensus"
    status: Literal[
        "confirmed", "customer_input_needed", "editorial_hypothesis", "rejected", "confidential", "unknown"
    ] = "editorial_hypothesis"
    blocking: bool = False
    basis: dict[str, list[str]] = Field(default_factory=dict)
    contextual_prompts: list[ContextualPrompt] = Field(default_factory=list)

    @field_validator("contextual_prompts")
    @classmethod
    def cap_prompts(cls, prompts: list[ContextualPrompt]) -> list[ContextualPrompt]:
        if len(prompts) > 2:
            raise ValueError("a Viewpoint Card may contain at most 2 contextual prompts")
        return prompts


class VisualizationSpec(BaseModel):
    visualization_id: str
    kind: Literal[
        "coverage_heatmap",
        "opportunity_quadrant",
        "buyer_journey",
        "format_distribution",
        "article_architecture",
        "evidence_provenance",
        "table_only",
    ]
    title: str
    reason: str
    input_fields: list[str] = Field(default_factory=list)
    fallback: str
    render: bool = True


class ReviewEnvelope(BaseModel):
    schema_version: int = 1
    review_type: Literal["brand_profile", "opportunity", "outline"]
    workspace: str
    brand: str
    run_id: str | None = None
    revision: int = Field(ge=1)
    input_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reviewer: str = ""
    reviewed_at: str = ""
    decisions: list[dict[str, Any]] = Field(default_factory=list)
