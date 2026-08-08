"""Deterministic mock LLMProvider.

Templates only — no paid API, no randomness. Generated copy is conservative by
construction: it never emits the editorially unsafe patterns (superlatives,
guarantees, no-review language) and only uses approved claim wording from the
brand fact ledger. Fixture keys draft_inject_text / metadata_inject_text let
tests drive claim-safety failure paths.
"""

from __future__ import annotations

from ..errors import PermanentProviderError, TransientProviderError
from .base import ProviderResult
from .fixtures import fingerprint, load_fixture, pop_failures

OUTLINE_COST = 0.01
DRAFT_COST = 0.02
META_COST = 0.01


def _allowed_claims(claims: list[dict]) -> list[dict]:
    return [c for c in claims if c["safety_level"] != "blocked" and c["decision"] == "approved"]


class MockLLMProvider:
    name = "mock"
    profile = "mock-llm"

    def __init__(self, profile: str = "mock-llm", fixture_dir: str | None = None) -> None:
        self.profile = profile
        self.fixture = load_fixture(fixture_dir, "llm")
        self.call_count = 0

    def _maybe_fail(self, op: str) -> None:
        # count every *attempt*, including ones that fail on fixture-injected
        # errors, so tests can assert "exactly one call, never retried"
        self.call_count += 1
        for kind in pop_failures(self.fixture, op, self.profile):
            if kind == "permanent":
                raise PermanentProviderError(self.name, f"fixture: permanent failure on {op}")
            raise TransientProviderError(self.name, f"fixture: transient failure on {op}")

    def _result(self, op: str, data: object, tokens: int) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            profile=self.profile,
            operation=op,
            data=data,
            request_fingerprint=fingerprint(op, self.profile),
            cost_estimate={"llm.outline": OUTLINE_COST, "llm.draft": DRAFT_COST, "llm.metadata": META_COST}[
                op
            ],
            token_estimate=tokens,
            source_confidence="mock",
        )

    # -- outline -----------------------------------------------------------

    def generate_outline(self, brief: dict, evidence: list[dict], claims: list[dict]) -> ProviderResult:
        self._maybe_fail("outline")
        primary = brief.get("primary_keyword") or (brief.get("seed_keywords") or ["topic"])[0]
        title = f"How to Choose a {primary.title()} Workflow That Fits Your Operation"[:59]
        evidence_ids = [e["evidence_id"] for e in evidence if e["opened_current_run"]][:8]
        source_map = ", ".join(evidence_ids) if evidence_ids else "n/a"
        approved = _allowed_claims(claims)
        product_lines = (
            "\n".join(f"- {c['claim_id']}: {c['allowed_wording']}" for c in approved[:3]) or "- n/a"
        )
        outline = f"""## Article Title: {title}

### Target Keywords:
- Primary: {primary}
- Secondary: {", ".join((brief.get("secondary_keywords") or [])[:3])}
- Long-tail: {", ".join((brief.get("longtail_keywords") or [])[:3])}

### Search Intent: {brief.get("intent", "MOFU")}

### Total Target Word Count: {brief.get("target_word_count", 1500)}

### Audience + Pain + Differentiator Thesis:
- Audience: {brief.get("audience") or "operators"}
- Core pain: workflow failure and tool-switching friction
- Differentiator: decision logic instead of a feature list

### Editorial Angle:
- One-sentence angle: {title}
- Why this article should beat current SERP: readers get failure diagnosis and tradeoffs, not a recap

### Google Already Knows (AIO/SERP Consensus):
- generic workflow advice
- a list of common tools

### Information Gain Plan:
- Avoid repeating: basic definitions
- Add instead: decision thresholds and not-a-fit boundaries
- Primary article delta: workflow-native operator guidance

### Pain Point -> Capability Narrative:
- Pain point 1: operators cannot tell which approach fits their footage
- Why existing workflow breaks: one-size-fits-all advice
- {brief.get("brand_name", "the product")} angle: {product_lines}
- Not-a-fit boundary: when manual editing still wins

### Section 1: Hook / Introduction (~150 words)
- Opening angle: the workflow failure moment
- Primary keyword placement: within the first 100 words
- Source map: {source_map}

### Section 2: Diagnose Your Input (H2, ~300 words)
- Key points: footage types; failure modes; review thresholds
- Source map: {source_map}

### Section 3: Decide Between Approaches (H2, ~350 words)
- Key points: comparison criteria; tradeoffs; when each approach fails
- Source map: {source_map}

### Section 4: How It Works in the Product (H2, ~300 words)
- Key points: input; option selection; review step; output
- Source map: {source_map}

### Section 5: When It Is Not the Right Fit (H2, ~250 words)
- Key points: not-a-fit boundaries; honest limitations

### Section N: Conclusion + CTA (~150 words)
- Summary of key takeaway
- End with a sharper restatement of the thesis

### FAQ Section (for schema markup):
- Q1: Which workflow fits my footage?
- Q2: When does the automated approach need review?
- Q3: What should I check before publishing?
"""
        return self._result("llm.outline", {"markdown": outline}, 900)

    # -- draft -------------------------------------------------------------

    def generate_draft(self, outline: str, claims: list[dict]) -> ProviderResult:
        self._maybe_fail("draft")
        approved = _allowed_claims(claims)
        mention = ""
        if approved:
            positioning = "\n".join(f"- {c['claim_id']}: {c['allowed_wording']}" for c in approved)
            mention = (
                "Approved product positioning for this piece, quoted from the brand fact"
                f" ledger:\n{positioning}\n"
            )
        body = f"""Every editing workflow starts from a real failure: footage that needs different
treatment per segment, no shared review step, and no honest boundary for when an
automated pass is enough. The fix is not another tool list — it is a decision
sequence you apply before you touch a timeline.

Diagnose your input first. Classify each segment by footage type and by what
breaks: text cut off, subject drift, audio that does not match the cut. Keep a
review step after every automated pass; treat the first pass as a starting
point, not a deliverable.

{mention}
The comparison matters less than the boundary. Automated passes trade control
for speed; manual passes trade speed for control. Decide per segment, and write
down the failure mode each method cannot solve.

When the workflow is a poor fit — unusual aspect ratios, heavily stylized
footage, or content that must match a brand review — stay manual. The product
is one option among several; it does not replace the review step.

The sharper takeaway: your workflow fails where the decision logic is missing.
Build the diagnosis step first, and the rest of the pipeline gets easier.
"""
        inject = self.fixture.get("draft_inject_text")
        if inject:
            body = f"{body}\n\n{inject}"
        return self._result("llm.draft", {"markdown": body}, 700)

    # -- metadata ----------------------------------------------------------

    def generate_metadata(self, outline: str, draft: str, paa_pool: list[str]) -> ProviderResult:
        self._maybe_fail("metadata")
        meta_title = self.fixture.get("meta_title") or "Editing Workflow Decisions: A Practical Guide"
        description = (
            self.fixture.get("meta_description")
            or "A practical guide to choosing an editing workflow, with decision logic, review"
            " steps, and honest not-a-fit boundaries."
        )
        slug = self.fixture.get("slug") or "editing-workflow-decisions"
        faq = self.fixture.get("faq") or [
            {
                "q": "Which workflow fits my footage?",
                "a": "Classify each segment by failure mode, then apply the decision sequence"
                " from the article.",
            },
            {
                "q": "When does the automated approach need review?",
                "a": "Every automated pass needs a review step before output is treated as final.",
            },
        ]
        data = {
            "meta_title": meta_title,
            "meta_description": description,
            "slug": slug,
            "faq": faq,
            "internal_links": [
                {"title": t, "anchor": a}
                for t, a in [
                    ("Editorial operations guide", "review workflow"),
                    ("Choosing tools by use case", "decision criteria"),
                ]
            ],
            "image_alt_texts": ["Workflow decision table comparing manual and automated passes"],
        }
        inject = self.fixture.get("metadata_inject_text")
        if inject:
            data["meta_description"] = f"{description} {inject}"
        return self._result("llm.metadata", data, 250)
