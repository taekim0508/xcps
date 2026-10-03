"""Module 2A (queue-based controller): selection rule, tie handling, recorded reasons."""

import pytest

from traffic_cps.contracts import DecisionReason, DecisionSource, Direction, HoldReason
from traffic_cps.controllers.queue import QueueController
from traffic_cps.runtime import hold_reason
from tests.unit.factories import context, observation

NS, WE = Direction.NS, Direction.WE


def select(**kw):
    return QueueController().select(context(observation(**kw)), "dec-1")


def test_module2A_higher_queue_score_determines_request():
    d = select(ns_score=0.24, we_score=0.62, ns_count=8, we_count=20)
    assert d.requested is WE and d.reason is DecisionReason.HIGHER_QUEUE_SCORE
    assert d.source is DecisionSource.QUEUE_RULE and not d.tie and not d.retains


def test_module2A_higher_score_on_current_green_retains():
    d = select(ns_score=1.1, we_score=0.4, ns_count=30, we_count=10)
    assert d.requested is NS and d.retains and d.reason is DecisionReason.HIGHER_QUEUE_SCORE


@pytest.mark.parametrize("green", [NS, WE])
def test_module2A_equal_scores_retain_current_green_regardless_of_order(green):
    d = select(ns_score=0.5, we_score=0.5, ns_count=15, we_count=15, green=green)
    assert d.requested is green and d.tie and d.reason is DecisionReason.EQUAL_QUEUE_SCORES


def test_module2A_both_zero_is_a_tie_that_retains():
    d = select(ns_score=0.0, we_score=0.0)
    assert d.requested is NS and d.tie and not d.both_at_cap


def test_module2A_tie_tolerance_is_absolute_1e9_and_inclusive():
    within = select(ns_score=0.5, we_score=0.5 + 1e-9, we_count=1)
    beyond = select(ns_score=0.5, we_score=0.5 + 2e-9, we_count=1)
    assert within.tie and within.requested is NS
    assert not beyond.tie and beyond.requested is WE


def test_module2A_saturated_tie_is_flagged():
    d = select(ns_score=2.0, we_score=2.0 - 5e-10, ns_count=70, we_count=70)
    assert d.tie and d.both_at_cap and d.requested is NS


def test_module2A_service_limit_with_competing_demand_beats_tie_and_preference():
    tie = select(ns_score=0.3, we_score=0.3, ns_count=9, we_count=9, elapsed=60)
    assert tie.requested is WE and tie.reason is DecisionReason.SERVICE_LIMIT_COMPETING_DEMAND
    preferred = select(ns_score=1.5, we_score=0.1, ns_count=40, we_count=1, elapsed=75)
    assert preferred.requested is WE and preferred.reason is DecisionReason.SERVICE_LIMIT_COMPETING_DEMAND


def test_module2A_no_forced_switch_without_competing_demand():
    d = select(ns_score=0.8, we_score=0.0, ns_count=20, we_count=0, elapsed=90)
    assert d.requested is NS and d.reason is DecisionReason.HIGHER_QUEUE_SCORE


def test_module2A_decision_records_unrounded_evidence():
    d = select(ns_score=0.123456789123, we_score=0.9, ns_count=4, we_count=29, elapsed=12)
    assert d.scores[NS] == 0.123456789123
    assert d.counts == {NS: 4, WE: 29}
    assert d.elapsed_green_s == 12 and d.green_service_limit_s == 60
    assert d.observation_id == "obs-10" and d.decision_id == "dec-1"
    assert d.epsilon is None and d.policy_revision is None


def test_module2A_refuses_to_decide_without_a_free_choice():
    ctx = context(observation(elapsed=3))
    with pytest.raises(ValueError):
        QueueController().select(ctx, "dec-1")


# --- runtime timing gate (priorities 1 and 2 of the operating table) ---------

def test_module2A_minimum_green_takes_precedence_over_higher_score():
    obs = observation(ns_score=0.0, we_score=1.9, we_count=60, elapsed=4)
    assert hold_reason(obs, minimum_green_s=5) is HoldReason.MINIMUM_GREEN_NOT_REACHED


def test_module2A_transition_in_progress_is_a_hold():
    assert hold_reason(observation(target=WE), minimum_green_s=5) is HoldReason.TRANSITION_IN_PROGRESS


def test_module2A_free_choice_at_exact_minimum_green():
    assert hold_reason(observation(elapsed=5), minimum_green_s=5) is None
