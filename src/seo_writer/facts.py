"""Brand fact-pack import and approval invalidation."""

from __future__ import annotations

import json

from . import state_machine as sm
from .db import Database
from .errors import UsageError
from .ids import hash_payload
from .models import FactsYaml


def import_facts(db: Database, brand: dict, facts: FactsYaml) -> dict:
    """Store rules + material terms as a new snapshot; invalidate stale approvals.

    Any run of this brand whose approvals reference an older facts hash is
    demoted to outline_pending_approval with an audited reason. Runs keep their
    immutable snapshots; they simply must be re-approved.
    """
    if facts.brand != brand["slug"]:
        raise UsageError(
            f"facts.yaml declares brand '{facts.brand}' but was imported for brand '{brand['slug']}'"
        )
    payload = facts.model_dump()
    snapshot_hash = hash_payload(payload)
    db.replace_fact_rules(brand["id"], [r.model_dump() for r in facts.rules], facts.facts_version)
    db.add_fact_snapshot(
        brand["id"], snapshot_hash, facts.facts_version, json.dumps(payload, ensure_ascii=False)
    )
    db.add_audit(
        None,
        "facts.imported",
        {"brand": brand["slug"], "facts_version": facts.facts_version, "facts_hash": snapshot_hash},
    )

    stale = db.brands_with_stale_approvals(brand["id"], snapshot_hash)
    for run in stale:
        db.set_status(run["id"], sm.OUTLINE_PENDING, step=run["step"], failure_reason=None)
        db.add_audit(
            run["id"],
            "approval.invalidated",
            {"reason": "facts changed after approval", "new_facts_hash": snapshot_hash},
        )
    return {
        "brand": brand["slug"],
        "facts_version": facts.facts_version,
        "facts_hash": snapshot_hash,
        "rules": len(facts.rules),
        "material_terms": len(facts.material_terms),
        "approvals_invalidated": len(stale),
    }
