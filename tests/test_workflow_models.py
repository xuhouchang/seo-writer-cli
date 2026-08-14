from __future__ import annotations

import pytest
from pydantic import ValidationError

from seo_writer.models import (
    ContentMap,
    ContextualPrompt,
    CoverageScore,
    ReviewEnvelope,
    TopicYaml,
    ViewpointCard,
)


def test_legacy_topic_yaml_remains_valid_and_new_gap_fields_default() -> None:
    topic = TopicYaml.model_validate(
        {"slug": "legacy", "title": "Legacy", "seed_keywords": ["legacy keyword"]}
    )
    assert topic.primary_job_to_be_done == ""
    assert topic.buyer_questions == []
    assert topic.candidate_competitors == []
    assert topic.desired_formats == []
    assert topic.gap_hypothesis == ""


def test_nonzero_coverage_requires_current_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence_refs"):
        CoverageScore(concept_explanation=2)


def test_content_map_requires_buyer_and_competitor_evidence_for_gap() -> None:
    with pytest.raises(ValidationError, match="buyer_evidence_refs"):
        ContentMap.model_validate(
            {
                "run_id": "run-1",
                "sample_scope": {"query_count": 3, "domain_count": 1, "opened_page_count": 1},
                "topics": [{"topic_id": "TOPIC-01", "label": "Workflow"}],
                "pages": [],
                "gaps": [
                    {
                        "gap_id": "GAP-01",
                        "gap_type": "topic",
                        "statement": "A sampled gap",
                        "buyer_evidence_refs": [],
                        "competitor_evidence_refs": ["SERP-01"],
                    }
                ],
            }
        )


def test_behavioral_prompts_are_outline_bound_and_capped() -> None:
    with pytest.raises(ValidationError):
        ContextualPrompt(prompt_id="PROMPT-01", prompt="Tell us a story", response_type="case")

    with pytest.raises(ValidationError, match="at most 2"):
        ViewpointCard(
            viewpoint_id="VP-01",
            section_id="SEC-01",
            statement="A test hypothesis",
            contextual_prompts=[
                ContextualPrompt(
                    prompt_id=f"PROMPT-{i}",
                    section_id="SEC-01",
                    viewpoint_id="VP-01",
                    prompt="What happens in practice?",
                    response_type="experience",
                )
                for i in range(3)
            ],
        )


def test_review_envelope_requires_sha256_input_hash() -> None:
    with pytest.raises(ValidationError, match="input_hash"):
        ReviewEnvelope(
            review_type="outline",
            workspace="test",
            brand="brand",
            run_id="run-1",
            revision=1,
            input_hash="stale",
        )
