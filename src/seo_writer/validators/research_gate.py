"""Research gate validator — migrated from the Skill's validate_research_gate.py.

Semantics are unchanged: only evidence actually opened in the current run with
a valid body fetch method counts; snippets, structured discovery and prior-run
reuse never count as current-run reading. Thresholds come from policy and may
not be weakened below the Skill floor (enforced in models.PolicyYaml).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import FETCH_METHODS, ResearchGatePolicy

QUERY_METHODS = {"real_page", "structured_api", "mock_api"}
NON_OPENED_METHODS = {"snippet_only"}
BAD_GRADES = {"promotional", "excluded"}
REQUIRED_QUERY_FIELDS = (
    "query",
    "timestamp",
    "location_language_device",
    "observation_method",
    "aio_visible",
)


@dataclass
class ResearchGateReport:
    min_queries: int
    query_count: int = 0
    serp_valid: int = 0
    serp_opened: int = 0
    threads_valid: int = 0
    threads_opened: int = 0
    subreddits: int = 0
    second_platform: bool = False
    second_platform_insufficiency_documented: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "counts": {
                "queries": self.query_count,
                "min_queries": self.min_queries,
                "opened_serp_pages": self.serp_opened,
                "min_opened_serp_pages": 0,  # filled by caller for display
                "opened_threads": self.threads_opened,
                "min_opened_threads": 0,
                "subreddits": self.subreddits,
                "min_subreddits": 0,
                "second_platform": self.second_platform,
                "second_platform_insufficiency_documented": self.second_platform_insufficiency_documented,
            },
            "errors": self.errors,
        }


def _is_valid_fetch(method: str) -> bool:
    return method in FETCH_METHODS


def _is_opened(row: dict[str, Any]) -> bool:
    return bool(row.get("opened_current_run")) and row.get("evidence_origin") == "current_run"


def _has_field(details: dict[str, Any], name: str) -> bool:
    """Field must be present and non-null; an explicit False (e.g. aio_visible)
    is a valid observation and must not be treated as missing."""
    if name not in details:
        return False
    value = details[name]
    return value is not None and value != ""


def evaluate(evidence: list[dict[str, Any]], policy: ResearchGatePolicy) -> ResearchGateReport:
    """Pure gate over evidence rows; returns a report with human gaps."""
    report = ResearchGateReport(min_queries=policy.min_queries)

    queries = [r for r in evidence if r["source_type"] == "search_query"]
    valid_queries = [
        r
        for r in queries
        if r.get("details", {}).get("query_method") in QUERY_METHODS
        and all(_has_field(r.get("details", {}), f) for f in REQUIRED_QUERY_FIELDS)
    ]
    report.query_count = len(valid_queries)
    if report.query_count < policy.min_queries:
        report.errors.append(
            f"need at least {policy.min_queries} Google query records "
            f"(found {report.query_count}); real page or structured/mock API observation required"
        )
    for q in queries:
        missing = [f for f in REQUIRED_QUERY_FIELDS if not _has_field(q.get("details", {}), f)]
        if missing:
            report.errors.append(
                f"query record {q['evidence_id']} lacks required fields: {', '.join(missing)}"
            )

    serp = [r for r in evidence if r["source_type"] == "serp_page"]
    report.serp_valid = sum(_is_valid_fetch(r["fetch_method"]) for r in serp)
    report.serp_opened = sum(
        _is_opened(r) and _is_valid_fetch(r["fetch_method"]) and r["fetch_method"] not in NON_OPENED_METHODS
        for r in serp
    )
    for r in serp:
        if not _is_valid_fetch(r["fetch_method"]):
            report.errors.append(f"SERP source {r['evidence_id']} has missing or invalid fetch method")
    if report.serp_opened < policy.min_opened_serp_pages:
        report.errors.append(
            f"need at least {policy.min_opened_serp_pages} current-run opened SERP pages "
            f"(found {report.serp_opened})"
        )

    threads = [r for r in evidence if r["source_type"] == "community_thread"]
    report.threads_valid = sum(_is_valid_fetch(r["fetch_method"]) for r in threads)
    opened_threads = [
        r
        for r in threads
        if _is_opened(r)
        and _is_valid_fetch(r["fetch_method"])
        and r["fetch_method"] not in NON_OPENED_METHODS
        and (r.get("grade") or "").lower() not in BAD_GRADES
    ]
    report.threads_opened = len(opened_threads)
    for r in threads:
        if not _is_valid_fetch(r["fetch_method"]):
            report.errors.append(f"community source {r['evidence_id']} has missing or invalid fetch method")
    if report.threads_opened < policy.min_opened_threads:
        report.errors.append(
            f"need at least {policy.min_opened_threads} current-run opened non-promotional community threads "
            f"(found {report.threads_opened})"
        )

    import re

    subreddits = {
        m.group(0).lower()
        for r in opened_threads
        for m in [re.search(r"r/[A-Za-z0-9_]+", r.get("platform") or "", re.I)]
        if m
    }
    report.subreddits = len(subreddits)
    if report.subreddits < policy.min_subreddits:
        report.errors.append(
            f"need current-run community evidence from at least {policy.min_subreddits} subreddits "
            f"(found {report.subreddits})"
        )

    report.second_platform = any("reddit" not in (r.get("platform") or "").lower() for r in opened_threads)
    report.second_platform_insufficiency_documented = any(
        r.get("details", {}).get("second_platform_search_outcome") == "insufficient" for r in evidence
    )
    if (
        policy.require_second_platform
        and not report.second_platform
        and not report.second_platform_insufficiency_documented
    ):
        report.errors.append(
            "need a second community platform (opened thread outside Reddit) or a documented "
            "second-platform search insufficiency"
        )
    return report
