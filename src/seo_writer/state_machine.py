"""ArticleRun state machine and step authorization.

The transition table below implements the product contract exactly:

    created -> researching -> research_gate_passed -> outline_pending_approval
    -> approved -> drafting -> completed -> exported
    researching / drafting -> blocked
    blocked -> researching (evidence or provider remediation)
    blocked -> outline_pending_approval (approval remediation only)
"""

from __future__ import annotations

from .errors import StateTransitionError

CREATED = "created"
RESEARCHING = "researching"
GATE_PASSED = "research_gate_passed"
OUTLINE_PENDING = "outline_pending_approval"
APPROVED = "approved"
DRAFTING = "drafting"
COMPLETED = "completed"
EXPORTED = "exported"
BLOCKED = "blocked"

STATES = frozenset(
    {CREATED, RESEARCHING, GATE_PASSED, OUTLINE_PENDING, APPROVED, DRAFTING, COMPLETED, EXPORTED, BLOCKED}
)

# Allowed transitions per source state.
TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({RESEARCHING}),
    RESEARCHING: frozenset({GATE_PASSED, BLOCKED}),
    GATE_PASSED: frozenset({OUTLINE_PENDING, RESEARCHING, BLOCKED}),
    OUTLINE_PENDING: frozenset({APPROVED, RESEARCHING, OUTLINE_PENDING}),
    APPROVED: frozenset({DRAFTING, OUTLINE_PENDING}),
    DRAFTING: frozenset({COMPLETED, BLOCKED}),
    COMPLETED: frozenset({EXPORTED, DRAFTING}),
    EXPORTED: frozenset({EXPORTED}),
    BLOCKED: frozenset({RESEARCHING, OUTLINE_PENDING, DRAFTING}),
}

# Which steps may run from which status.
STEP_AUTHORIZED: dict[str, frozenset[str]] = {
    "research": frozenset({CREATED, RESEARCHING, GATE_PASSED, OUTLINE_PENDING, BLOCKED}),
    "validate_research": frozenset({RESEARCHING, GATE_PASSED, BLOCKED}),
    "outline": frozenset({GATE_PASSED, OUTLINE_PENDING, APPROVED, DRAFTING, COMPLETED, BLOCKED}),
    "gap_map": frozenset({RESEARCHING, GATE_PASSED, OUTLINE_PENDING, APPROVED}),
    "render": frozenset(STATES - {CREATED}),
    "import_review": frozenset({OUTLINE_PENDING, APPROVED}),
    "approve": frozenset({OUTLINE_PENDING, APPROVED}),
    "draft": frozenset({OUTLINE_PENDING, APPROVED, DRAFTING, COMPLETED, BLOCKED}),
    "metadata": frozenset({OUTLINE_PENDING, DRAFTING, COMPLETED, BLOCKED}),
    "validate": frozenset(STATES - {CREATED}),
    "export": frozenset({COMPLETED, EXPORTED, BLOCKED}),
    "retry": frozenset(STATES - {CREATED}),
}

# Retry --step: which statuses may resume into which state for a given step.
RETRY_RESUME = {
    "research": {
        CREATED: RESEARCHING,
        RESEARCHING: RESEARCHING,
        GATE_PASSED: RESEARCHING,
        OUTLINE_PENDING: RESEARCHING,
        BLOCKED: RESEARCHING,
    },
    "outline": {OUTLINE_PENDING: OUTLINE_PENDING, BLOCKED: OUTLINE_PENDING, GATE_PASSED: OUTLINE_PENDING},
    "draft": {BLOCKED: DRAFTING, APPROVED: DRAFTING, COMPLETED: DRAFTING, DRAFTING: DRAFTING},
}


def assert_transition(current: str, target: str) -> None:
    if current not in STATES or target not in STATES:
        raise StateTransitionError(f"unknown state: {current or target}")
    if target not in TRANSITIONS.get(current, frozenset()):
        raise StateTransitionError(f"transition not allowed: {current} -> {target}")


def assert_step_authorized(step: str, status: str) -> None:
    allowed = STEP_AUTHORIZED.get(step)
    if allowed is None:
        raise StateTransitionError(f"unknown step: {step}")
    if status not in allowed:
        raise StateTransitionError(f"step '{step}' is not authorized from status '{status}'")
