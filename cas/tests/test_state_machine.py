"""Unit tests for the contract state machine (spec §3)."""
import pytest

from cas.domain import states
from cas.domain.errors import InvalidTransition

SPEC_TABLE = {
    ("proposed", "ready"), ("proposed", "blocked"), ("ready", "leased"), ("leased", "running"),
    ("leased", "blocked"), ("running", "blocked"), ("running", "submitted"), ("submitted", "validating"),
    ("validating", "accepted"), ("validating", "rejected"), ("leased", "failed"), ("running", "failed"),
    ("blocked", "ready"), ("rejected", "ready"), ("failed", "ready"),
}
CANCELLABLE = {(s, "cancelled") for s in states.NONTERMINAL}
DOCUMENTED_ADDITION = {("ready", "blocked")}


def test_allowed_transitions_are_exactly_the_spec_table_plus_one_documented_addition():
    allowed = {(src, dst) for src, dsts in states.CONTRACT_TRANSITIONS.items() for dst in dsts}
    assert allowed == SPEC_TABLE | CANCELLABLE | DOCUMENTED_ADDITION


@pytest.mark.parametrize("src", states.ALL_STATES)
@pytest.mark.parametrize("dst", states.ALL_STATES)
def test_every_unlisted_transition_is_refused(src, dst):
    if dst in states.CONTRACT_TRANSITIONS[src]:
        states.check_transition(src, dst)
    else:
        with pytest.raises(InvalidTransition) as err:
            states.check_transition(src, dst)
        assert err.value.status == 409 and err.value.code == "INVALID_TRANSITION"


def test_accepted_and_cancelled_are_terminal():
    assert not states.CONTRACT_TRANSITIONS["accepted"]
    assert not states.CONTRACT_TRANSITIONS["cancelled"]
