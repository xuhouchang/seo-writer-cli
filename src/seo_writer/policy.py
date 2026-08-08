"""Brand policy import and approval invalidation.

A run snapshots the policy hash at creation time. Importing a new policy.yaml
for the brand demotes every run whose approvals were granted under an older
policy hash back to outline_pending_approval with an audited reason — the same
contract as fact-pack changes (policy changes invalidate approvals).
"""

from __future__ import annotations

import json

from . import state_machine as sm
from .db import Database
from .errors import UsageError
from .ids import hash_payload
from .models import PolicyYaml


def import_policy(db: Database, brand: dict, policy: PolicyYaml) -> dict:
    if policy.brand != brand["slug"]:
        raise UsageError(
            f"policy.yaml declares brand '{policy.brand}' but was imported for brand '{brand['slug']}'"
        )
    payload = policy.model_dump()
    policy_hash = hash_payload(payload)
    db.set_policy(brand["id"], json.dumps(payload, ensure_ascii=False), policy_hash)
    db.add_audit(None, "policy.imported", {"brand": brand["slug"], "policy_hash": policy_hash})

    stale = db.runs_with_stale_policy(brand["id"], policy_hash)
    for run in stale:
        db.set_status(run["id"], sm.OUTLINE_PENDING, step=run["step"], failure_reason=None)
        db.add_audit(
            run["id"],
            "approval.invalidated",
            {"reason": "policy changed after run creation", "new_policy_hash": policy_hash},
        )
    return {
        "brand": brand["slug"],
        "policy_hash": policy_hash,
        "approvals_invalidated": len(stale),
    }
