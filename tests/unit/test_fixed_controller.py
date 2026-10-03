"""Module 8 / Section J (fixed-time baseline): schedule rule and `fixed_schedule` attribution."""

import pytest

from traffic_cps.contracts import DecisionReason, DecisionSource, Direction
from traffic_cps.controllers.fixed import FixedTimeController
from tests.unit.factories import context, observation

NS, WE = Direction.NS, Direction.WE


def select(target=30, **kw):
    return FixedTimeController(target).select(context(observation(**kw)), "dec-1")


def test_module8_fixed_retains_below_target():
    d = select(elapsed=29)
    assert d.requested is NS and d.retains
    assert d.source is DecisionSource.FIXED_SCHEDULE and d.reason is DecisionReason.FIXED_TARGET_NOT_REACHED
    assert d.fixed_green_target_s == 30 and d.elapsed_green_s == 29


@pytest.mark.parametrize("elapsed", [30, 33, 55])
def test_module8_fixed_switches_at_or_after_target(elapsed):
    d = select(elapsed=elapsed)
    assert d.requested is WE and not d.retains
    assert d.source is DecisionSource.FIXED_SCHEDULE and d.reason is DecisionReason.FIXED_TARGET_REACHED


@pytest.mark.parametrize("green", [NS, WE])
def test_module8_fixed_is_symmetric(green):
    assert select(green=green, elapsed=30).requested is green.other
    assert select(green=green, elapsed=10).requested is green


def test_module8_fixed_ignores_queue_scores():
    # Heavy competing demand does not shorten the green; no competing demand does not extend it.
    assert select(elapsed=20, ns_score=0.0, we_score=1.9, ns_count=0, we_count=60).requested is NS
    assert select(elapsed=30, ns_score=1.9, we_score=0.0, ns_count=60, we_count=0).requested is WE
    assert not select(elapsed=30, ns_score=0.5, we_score=0.5).tie


def test_module8_fixed_records_scores_and_counts_as_context():
    d = select(elapsed=30, ns_score=0.24, we_score=0.62, ns_count=8, we_count=20)
    assert d.scores == {NS: 0.24, WE: 0.62} and d.counts == {NS: 8, WE: 20}
    assert d.epsilon is None and d.policy_revision is None


@pytest.mark.parametrize("target", [60, 75])
def test_sectionJ_fixed_target_at_or_above_service_limit_rejected(target):
    with pytest.raises(ValueError, match="strictly below"):
        select(target=target, elapsed=30)


@pytest.mark.parametrize("target", [0, -5, 2.5, True])
def test_module8_fixed_target_must_be_positive_integer(target):
    with pytest.raises(ValueError, match="positive integer"):
        FixedTimeController(target)


def test_module8_fixed_refuses_non_free_choice():
    with pytest.raises(ValueError, match="without a free choice"):
        FixedTimeController(30).select(context(observation(target=WE)), "dec-1")
    with pytest.raises(ValueError, match="without a free choice"):
        select(elapsed=3)
