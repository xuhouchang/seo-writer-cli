"""Claim-safety validator — migrated from the Skill's validate_claim_safety.py.

Generic editorial unsafe patterns (superlatives, guarantees, no-review
language) are built in exactly as before. Brand-specific blocked wording and
material-term -> claim-id mapping come from the per-brand fact ledger
(facts.yaml), which is how the Skill's customer-specific ledger generalizes.
"""

from __future__ import annotations

import re
from typing import Any

# Generic editorial patterns inherited verbatim from the Skill validator.
DEFAULT_UNSAFE_PATTERNS: dict[str, str] = {
    "superlative": r"\b(world'?s first|best(?:\s+\w+){0,2}|leading)\b",
    "guarantee": (
        r"\b(guarantee[sd]?|always saves?|save \d+ (?:hours?|minutes?)"
        r"|fully automated|hands[- ]off)\b"
    ),
    "no-review": r"\b(no human review|without (?:any )?human review|review[- ]free|no review required)\b",
}

OUTLINE_REQUIRED_MARKERS = ("## Article Title", "### Target Keywords", "### Search Intent", "### Section 1")


def _corpus(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def check_unsafe_wording(corpus: str, brand_rules: list[dict[str, Any]]) -> list[str]:
    """Generic patterns plus every disallowed wording from blocked brand claims."""
    errors: list[str] = []
    for name, pattern in DEFAULT_UNSAFE_PATTERNS.items():
        if re.search(pattern, corpus, re.I):
            errors.append(f"unsafe wording detected: {name}")
    for rule in brand_rules:
        if rule["safety_level"] == "blocked":
            for wording in rule.get("disallowed_wording", []):
                if wording and re.search(re.escape(wording), corpus, re.I):
                    errors.append(f"blocked claim wording detected: {rule['claim_id']} ('{wording}')")
    return errors


def check_material_claims(
    corpus: str, material_terms: list[dict[str, Any]], approved_claim_ids: set[str]
) -> list[str]:
    """Any material term used in copy must have an approved claim-id entry.

    The error names the full offending fragment from the copy, so an author can
    find the exact sentence that needs editing.
    """
    errors: list[str] = []
    for item in material_terms:
        term, claim_id = item["term"], item["claim_id"]
        if not re.search(term, corpus, re.I) or claim_id in approved_claim_ids:
            continue
        fragment = term
        for line in corpus.splitlines():
            if re.search(term, line, re.I):
                fragment = line.strip()[:80]
                break
        errors.append(f"material claim '{term}' has no approved {claim_id} entry (found: '{fragment}')")
    return errors


def usable_claim_ids(rules: list[dict[str, Any]]) -> set[str]:
    return {r["claim_id"] for r in rules if r["safety_level"] != "blocked" and r["decision"] == "approved"}


def validate_outline_structure(outline: str) -> list[str]:
    errors: list[str] = []
    if not outline.strip():
        return ["outline is empty"]
    for marker in OUTLINE_REQUIRED_MARKERS:
        if marker not in outline:
            errors.append(f"outline lacks required section: {marker}")
    return errors


def validate_metadata_lengths(meta: dict[str, Any]) -> list[str]:
    """SEO metadata hard length rules from the Skill's meta reference."""
    errors: list[str] = []
    title = (meta.get("meta_title") or "").strip()
    description = (meta.get("meta_description") or "").strip()
    if not title:
        errors.append("metadata: meta title is missing")
    elif len(title) > 60:
        errors.append(f"metadata: meta title exceeds 60 chars ({len(title)})")
    if not description:
        errors.append("metadata: meta description is missing")
    elif len(description) > 155:
        errors.append(f"metadata: meta description exceeds 155 chars ({len(description)})")
    for i, alt in enumerate(meta.get("image_alt_texts", []) or []):
        if len(alt) > 125:
            errors.append(f"metadata: image alt text #{i + 1} exceeds 125 chars ({len(alt)})")
    slug = meta.get("slug") or ""
    if slug and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        errors.append(f"metadata: invalid slug '{slug}'")
    return errors


def validate_run_corpus(
    outline: str,
    article: str,
    metadata: str,
    brand_rules: list[dict[str, Any]],
    material_terms: list[dict[str, Any]],
) -> list[str]:
    """Everything that must hold before a run may be completed/exported."""
    errors: list[str] = []
    if not outline.strip():
        errors.append("missing outline")
    if not article.strip():
        errors.append("missing article draft")
    if not metadata.strip():
        errors.append("missing metadata")
    corpus = _corpus(outline, article, metadata)
    errors += check_unsafe_wording(corpus, brand_rules)
    errors += check_material_claims(corpus, material_terms, usable_claim_ids(brand_rules))
    return errors
