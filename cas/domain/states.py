"""Contract and goal state machines (spec §3). Anything not listed here is an invalid transition."""
from __future__ import annotations

from cas.domain.errors import InvalidTransition

PROPOSED, READY, LEASED, RUNNING, BLOCKED = "proposed", "ready", "leased", "running", "blocked"
SUBMITTED, VALIDATING, ACCEPTED, REJECTED, FAILED, CANCELLED = (
    "submitted", "validating", "accepted", "rejected", "failed", "cancelled")

ALL_STATES = (PROPOSED, READY, LEASED, RUNNING, BLOCKED, SUBMITTED, VALIDATING,
              ACCEPTED, REJECTED, FAILED, CANCELLED)
TERMINAL = frozenset({ACCEPTED, CANCELLED})
NONTERMINAL = frozenset(ALL_STATES) - TERMINAL
# States in which an agent may hold a lease.
LEASE_HELD = frozenset({LEASED, RUNNING})
# States with a live attempt whose pinned inputs matter.
ACTIVE = frozenset({READY, LEASED, RUNNING, SUBMITTED, VALIDATING})

# The spec's table, plus ready -> blocked. The reconciler pseudocode in §10 blocks any
# non-terminal contract whose pins are invalidated, which a ready contract can be.
CONTRACT_TRANSITIONS: dict[str, frozenset[str]] = {
    PROPOSED: frozenset({READY, BLOCKED, CANCELLED}),
    READY: frozenset({LEASED, BLOCKED, CANCELLED}),
    LEASED: frozenset({RUNNING, BLOCKED, FAILED, CANCELLED}),
    RUNNING: frozenset({SUBMITTED, BLOCKED, FAILED, CANCELLED}),
    BLOCKED: frozenset({READY, CANCELLED}),
    SUBMITTED: frozenset({VALIDATING, CANCELLED}),
    VALIDATING: frozenset({ACCEPTED, REJECTED, CANCELLED}),
    REJECTED: frozenset({READY, CANCELLED}),
    FAILED: frozenset({READY, CANCELLED}),
    ACCEPTED: frozenset(),
    CANCELLED: frozenset(),
}

BLOCK_REASONS = frozenset({"stale_input", "explicit_hold", "decision_hold", "policy_denied",
                           "dependency_unmet", "goal_revised"})


def can_transition(src: str, dst: str) -> bool:
    return dst in CONTRACT_TRANSITIONS.get(src, frozenset())


def check_transition(src: str, dst: str) -> None:
    if not can_transition(src, dst):
        raise InvalidTransition(
            f"contract cannot move from '{src}' to '{dst}'",
            details={"from": src, "to": dst, "allowed": sorted(CONTRACT_TRANSITIONS.get(src, ()))},
        )


GOAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"active", "cancelled"}),
    "active": frozenset({"paused", "completed", "cancelled"}),
    "paused": frozenset({"active", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


def check_goal_transition(src: str, dst: str) -> None:
    if dst not in GOAL_TRANSITIONS.get(src, frozenset()):
        raise InvalidTransition(f"goal cannot move from '{src}' to '{dst}'",
                                details={"from": src, "to": dst})
