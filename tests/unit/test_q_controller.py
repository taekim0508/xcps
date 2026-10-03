"""Module 2B selection: exploration branches, greedy tie rule, coverage-aware frozen evaluation."""

import pytest

from traffic_cps.config import load_config
from traffic_cps.contracts import DecisionReason, DecisionSource, Direction, FallbackReason, Mode, QState
from traffic_cps.controllers.q_learning import QLearningController
from traffic_cps.learning import Learner
from tests.unit.factories import context, observation

NS, WE = Direction.NS, Direction.WE
CONFIG = load_config("configs/default.yaml")
STATE = QState(0, 1, NS, 1)   # observation(ns_score=0.1, we_score=0.5, elapsed=10)


class FixedRng:
    """random() returns the given draws in order; choice() returns a given item."""

    def __init__(self, draws=(), choice=None):
        self.draws, self.picked, self.calls = list(draws), choice, []

    def random(self):
        self.calls.append("random")
        return self.draws.pop(0)

    def choice(self, seq):
        self.calls.append("choice")
        assert tuple(seq) == (NS, WE)        # uniform over the permitted actions, fixed order
        return self.picked


class NoRng:
    def random(self):
        raise AssertionError("evaluation must not consume the controller RNG")

    choice = random


def learner(values=None, counts=None, epsilon=None, rng=None):
    lr = Learner(CONFIG, rng=rng or NoRng())
    if values is not None:
        lr.table.ensure(STATE)
        for a in (NS, WE):
            lr.table._set(STATE, a, values[a], (counts or {NS: 1, WE: 1})[a])
    if epsilon is not None:
        lr.schedule.epsilon = epsilon
    return lr


def select(lr, mode, **kw):
    obs = observation(**{"ns_score": 0.1, "we_score": 0.5, "ns_count": 3, "we_count": 15, "elapsed": 10, **kw})
    return QLearningController(lr).select(context(obs, mode), "dec-1")


# ----------------------------------------------------------------------
# Training
# ----------------------------------------------------------------------

def test_module2B_training_explores_with_probability_epsilon():
    rng = FixedRng(draws=[0.3], choice=WE)
    d = select(learner(values={NS: 5.0, WE: -5.0}, epsilon=0.5, rng=rng), Mode.TRAIN)
    assert d.source is DecisionSource.Q_EXPLORATION and d.reason is DecisionReason.EXPLORATION
    assert d.requested is WE and d.epsilon == 0.5 and d.exploration_draw == 0.3
    assert rng.calls == ["random", "choice"]


def test_module2B_exploration_matching_greedy_is_still_exploration():
    d = select(learner(values={NS: 5.0, WE: -5.0}, epsilon=0.5, rng=FixedRng([0.1], NS)), Mode.TRAIN)
    assert d.requested is NS and d.source is DecisionSource.Q_EXPLORATION


def test_module2B_training_exploits_when_draw_not_below_epsilon():
    rng = FixedRng(draws=[0.5])
    d = select(learner(values={NS: -3.0, WE: -1.0}, epsilon=0.5, rng=rng), Mode.TRAIN)
    assert d.source is DecisionSource.Q_GREEDY and d.reason is DecisionReason.HIGHER_Q_VALUE
    assert d.requested is WE and rng.calls == ["random"]       # no choice() draw when exploiting


def test_module2B_greedy_tie_retains_current_green_with_abs_tolerance():
    tie = select(learner(values={NS: -1.0, WE: -1.0 + 1e-9}, epsilon=0.0, rng=FixedRng([0.0])), Mode.TRAIN)
    assert tie.requested is NS and tie.reason is DecisionReason.EQUAL_Q_VALUES and tie.tie
    beyond = select(learner(values={NS: -1.0, WE: -1.0 + 2e-9}, epsilon=0.0, rng=FixedRng([0.0])), Mode.TRAIN)
    assert beyond.requested is WE and beyond.reason is DecisionReason.HIGHER_Q_VALUE and not beyond.tie


def test_module2B_training_initializes_encountered_state_to_zero():
    lr = learner(epsilon=0.0, rng=FixedRng([0.9]))
    d = select(lr, Mode.TRAIN)
    assert lr.table.contains(STATE) and lr.table.values(STATE) == {NS: 0.0, WE: 0.0}
    assert d.requested is NS and d.reason is DecisionReason.EQUAL_Q_VALUES      # zero tie keeps green


def test_module2B_decision_records_decision_time_evidence():
    lr = learner(values={NS: -2.0, WE: -1.5}, counts={NS: 4, WE: 2}, epsilon=0.2, rng=FixedRng([0.7]))
    lr.table.revision = 9
    d = select(lr, Mode.TRAIN)
    assert d.q_state == STATE and d.q_values == {NS: -2.0, WE: -1.5} and d.q_update_counts == {NS: 4, WE: 2}
    assert d.policy_revision == 9 and d.epsilon == 0.2 and d.scores == {NS: 0.1, WE: 0.5}
    assert d.fallback_reason is None
    lr.table._set(STATE, WE, -99.0, 3)                                   # later learning...
    assert d.q_values == {NS: -2.0, WE: -1.5}                            # ...never alters the record


# ----------------------------------------------------------------------
# Frozen evaluation
# ----------------------------------------------------------------------

def test_module2B_evaluation_unseen_state_falls_back_to_queue_rule_without_insertion():
    lr = learner()
    d = select(lr, Mode.EVALUATE)
    assert d.source is DecisionSource.QUEUE_FALLBACK and d.fallback_reason is FallbackReason.UNSEEN_STATE
    assert d.reason is DecisionReason.HIGHER_QUEUE_SCORE and d.requested is WE     # queue rule's choice
    assert not lr.table.contains(STATE) and d.epsilon is None and d.exploration_draw is None


def test_module2B_evaluation_incomplete_coverage_falls_back():
    d = select(learner(values={NS: -5.0, WE: 0.0}, counts={NS: 3, WE: 0}), Mode.EVALUATE)
    assert d.source is DecisionSource.QUEUE_FALLBACK
    assert d.fallback_reason is FallbackReason.INCOMPLETE_ACTION_COVERAGE
    assert d.q_values == {NS: -5.0, WE: 0.0} and d.q_update_counts == {NS: 3, WE: 0}


def test_module2B_evaluation_full_coverage_exploits_without_rng_or_changes():
    lr = learner(values={NS: -1.0, WE: -4.0}, counts={NS: 2, WE: 1}, epsilon=0.9)
    before = lr.table.to_json()
    d = select(lr, Mode.EVALUATE)
    assert d.source is DecisionSource.Q_GREEDY and d.requested is NS and d.epsilon is None
    assert lr.table.to_json() == before and lr.schedule.epsilon == 0.9


def test_module2B_refuses_non_free_choice():
    with pytest.raises(ValueError, match="without a free choice"):
        select(learner(), Mode.TRAIN, elapsed=3)
