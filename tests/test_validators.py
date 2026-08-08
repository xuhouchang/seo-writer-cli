"""Pure-function unit tests for the migrated validators (research gate,
claim safety, metadata/outline structure)."""

from __future__ import annotations

from seo_writer.models import SKILL_GATE_FLOOR, ResearchGatePolicy
from seo_writer.validators.claim_safety import (
    check_material_claims,
    check_unsafe_wording,
    validate_metadata_lengths,
    validate_outline_structure,
    validate_run_corpus,
)
from seo_writer.validators.research_gate import evaluate as evaluate_gate

# ---------------------------------------------------------------------------
# research gate
# ---------------------------------------------------------------------------


def _opened_thread(platform: str = "Reddit / r/NewTubers", grade: str = "high", eid: str = "T-01") -> dict:
    return {
        "evidence_id": eid,
        "source_type": "community_thread",
        "fetch_method": "mock_reddit",
        "opened_current_run": True,
        "evidence_origin": "current_run",
        "platform": platform,
        "grade": grade,
    }


def _query(eid: str, details: dict | None = None) -> dict:
    base = {
        "query": "kw",
        "timestamp": "2026-08-08T00:00:00Z",
        "location_language_device": "US/en/desktop",
        "observation_method": "mock_api",
        "aio_visible": False,
    }
    d = dict(base)
    if details:
        d.update(details)
    return {
        "evidence_id": eid,
        "source_type": "search_query",
        "fetch_method": "mock_api",
        "opened_current_run": False,
        "evidence_origin": "structured_discovery",
        "details": {"query_method": "mock_api", **d},
    }


def _serp(eid: str, opened: bool = True, fetch: str = "mock_webfetch") -> dict:
    return {
        "evidence_id": eid,
        "source_type": "serp_page",
        "fetch_method": fetch,
        "opened_current_run": opened,
        "evidence_origin": "current_run" if opened else "structured_discovery",
    }


def _passing_evidence() -> list[dict]:
    rows = [_query(f"Q-{i:02d}") for i in range(1, 4)]
    rows += [_serp(f"S-{i:02d}") for i in range(1, 6)]
    subs = ["r/NewTubers", "r/YouTubeCreators", "r/ContentCreators", "r/VideoEditing"]
    # 10 opened threads across 4 subreddits (floor) + one second platform
    rows += [_opened_thread(f"Reddit / {subs[i % 4]}", eid=f"T-{i:02d}") for i in range(10)]
    rows += [_opened_thread("Quora / quora.com", eid="T-SP-01")]
    return rows


def test_gate_passes_with_minimum_evidence() -> None:
    report = evaluate_gate(_passing_evidence(), ResearchGatePolicy())
    assert report.passed, report.errors


def test_gate_counts_snippet_only_as_not_opened() -> None:
    rows = _passing_evidence()
    rows[3]["fetch_method"] = "snippet_only"  # SERP-01 becomes a snippet
    report = evaluate_gate(rows, ResearchGatePolicy())
    assert not report.passed
    assert any("opened SERP pages" in e for e in report.errors)


def test_gate_excludes_promotional_and_reused() -> None:
    rows = _passing_evidence()
    # rows = 3 queries + 5 serps, first community thread starts at index 8
    rows[8]["grade"] = "promotional"  # T-01 promotional -> not counted
    rows[9]["grade"] = "promotional"  # T-02 promotional -> not counted
    rows.append({**_opened_thread(eid="T-REUSED"), "evidence_origin": "reused_prior_run_evidence"})
    report = evaluate_gate(rows, ResearchGatePolicy())
    # 12 threads: 2 promotional + 1 reused excluded -> 9 opened < floor of 10
    assert report.threads_opened == 9
    assert not report.passed


def test_gate_second_platform_insufficiency_documented() -> None:
    rows = _passing_evidence()
    rows = [r for r in rows if "quora" not in (r.get("platform") or "").lower()]
    rows.append(
        {
            "evidence_id": "T-SP-SEARCH",
            "source_type": "community_thread",
            "fetch_method": "mock_api",
            "opened_current_run": False,
            "evidence_origin": "structured_discovery",
            "details": {"second_platform_search_outcome": "insufficient"},
        }
    )
    report = evaluate_gate(rows, ResearchGatePolicy())
    assert report.passed, report.errors
    assert report.second_platform_insufficiency_documented


def test_gate_query_aio_false_is_valid() -> None:
    """aio_visible=False is an observation, not a missing field."""
    rows = _passing_evidence()
    assert rows[0]["details"]["aio_visible"] is False
    report = evaluate_gate(rows, ResearchGatePolicy())
    assert report.query_count == 3


def test_gate_floor_defaults_match_skill() -> None:
    assert SKILL_GATE_FLOOR.min_queries == 3
    assert SKILL_GATE_FLOOR.min_opened_serp_pages == 5
    assert SKILL_GATE_FLOOR.min_opened_threads == 10
    assert SKILL_GATE_FLOOR.min_subreddits == 4
    assert SKILL_GATE_FLOOR.require_second_platform is True


# ---------------------------------------------------------------------------
# claim safety
# ---------------------------------------------------------------------------

RULES = [
    {
        "claim_id": "CLAIM-001",
        "claim": "review pass checks every segment",
        "safety_level": "safe",
        "allowed_wording": "the review pass checks every segment",
        "decision": "approved",
    },
    {
        "claim_id": "CLAIM-002",
        "claim": "saves every user four hours",
        "safety_level": "blocked",
        "disallowed_wording": ["saves every user", "four hours"],
        "decision": "blocked",
    },
]
MATERIAL_TERMS = [{"term": "four hours", "claim_id": "CLAIM-002"}]


def test_unsafe_wording_detects_skill_patterns() -> None:
    for bad in [
        "the world's first editor",
        "best tool on the market",
        "we guarantee results",
        "no human review",
    ]:
        hits = check_unsafe_wording(bad, RULES)
        assert hits, f"missed unsafe wording: {bad!r}"
    assert not check_unsafe_wording("the review pass checks every segment", RULES)


def test_unsafe_wording_honors_brand_blocked_disallowed() -> None:
    hits = check_unsafe_wording("saves every user four hours per video", RULES)
    assert any("CLAIM-002" in h for h in hits)


def test_material_term_requires_approved_claim() -> None:
    # term present -> claim_id must be approved; CLAIM-002 is blocked
    errors = check_material_claims("you save four hours per video", MATERIAL_TERMS, RULES)
    assert any("CLAIM-002" in e for e in errors)
    # approved claim is fine
    errors = check_material_claims("the review pass checks every segment", MATERIAL_TERMS, RULES)
    assert not errors


def test_validate_run_corpus_catches_blocked_claim_in_copy() -> None:
    errors = validate_run_corpus(
        "## Article Title: X\n", "Acme saves every user four hours per video.\n", "", RULES, MATERIAL_TERMS
    )
    assert any("CLAIM-002" in e for e in errors)
    assert any("four hours per video" in e for e in errors)


def test_outline_structure_required_sections() -> None:
    good = "## Article Title: X\n### Target Keywords:\n### Search Intent:\n### Section 1: Intro\n"
    assert not validate_outline_structure(good)
    errors = validate_outline_structure("## Nope\n")
    assert errors


def test_metadata_length_caps() -> None:
    bad = {
        "meta_title": "x" * 61,
        "meta_description": "y" * 156,
        "slug": "bad slug!",
        "image_alt_texts": ["z" * 126],
    }
    errors = validate_metadata_lengths(bad)
    assert any("meta title" in e for e in errors)
    assert any("meta description" in e for e in errors)
    assert any("slug" in e for e in errors)
    assert any("alt" in e for e in errors)
    good = {
        "meta_title": "x" * 60,
        "meta_description": "y" * 155,
        "slug": "ok-slug",
        "image_alt_texts": ["z" * 125],
    }
    assert not validate_metadata_lengths(good)
