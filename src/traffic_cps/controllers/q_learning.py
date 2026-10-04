"""Tabular Q-learning controller (Module 2B).

Only asked at a free choice; timing rules are enforced before it is called.

Training: one `random()` draw decides explore-or-not (explore when draw < epsilon);
only when exploring, `choice()` picks uniformly among the permitted actions. An
exploratory choice is labeled exploration even when it matches the greedy one.
Otherwise the highest value wins, with the current green kept on an absolute
1e-9 tie. Encountered states start at zero.

Evaluation: no randomness, no insertion, no learning. If the state is unseen or
a permitted action has no completed update, the queue rule decides and the
decision discloses why.

Decision evidence (state, values, update counts, revision, epsilon, draw) is
copied at selection time and never reflects later learning.
"""

from __future__ import annotations

from dataclasses import replace

from traffic_cps.contracts import (
    Decision,
    DecisionContext,
    DecisionReason,
    DecisionSource,
    Direction,
    FallbackReason,
    Mode,
)
from traffic_cps.controllers.queue import QueueController
from traffic_cps.learning import Learner


def greedy_choice(values, green: Direction, tol: float) -> tuple[Direction, DecisionReason, bool]:
    """Highest value wins; the current green wins an absolute-tolerance tie. Shared with the explainer."""
    other = green.other
    tie = abs(values[green] - values[other]) <= tol
    if tie:
        return green, DecisionReason.EQUAL_Q_VALUES, True
    return (green if values[green] > values[other] else other), DecisionReason.HIGHER_Q_VALUE, False


class QLearningController:
    name = "q_learning"

    def __init__(self, learner: Learner):
        self.learner = learner
        self._fallback = QueueController()

    def select(self, context: DecisionContext, decision_id: str) -> Decision:
        o = context.observation
        green = o.current_green
        if green is None or o.elapsed_green_s is None or o.elapsed_green_s < context.minimum_green_s:
            raise ValueError(f"Q-learning controller asked to decide at t={o.time} without a free choice")
        lr = self.learner
        state = lr.encode(o)
        allowed = tuple(context.allowed)
        training = context.mode is Mode.TRAIN

        if training:
            lr.table.ensure(state)
        values, counts = lr.table.values(state), lr.table.update_counts(state)
        evidence = dict(q_state=state, q_values=values, q_update_counts=counts,
                        policy_revision=lr.table.revision)

        if not training:
            fallback = None
            if not lr.table.contains(state):
                fallback = FallbackReason.UNSEEN_STATE
            elif any(counts[a] == 0 for a in allowed):
                fallback = FallbackReason.INCOMPLETE_ACTION_COVERAGE
            if fallback is not None:
                d = self._fallback.select(context, decision_id)
                return replace(d, source=DecisionSource.QUEUE_FALLBACK, fallback_reason=fallback, **evidence)

        epsilon = draw = None
        if training:
            epsilon = lr.schedule.epsilon
            draw = lr.rng.random()
        greedy, greedy_reason, tie = greedy_choice(values, green, context.tie_abs_tolerance)
        if training and draw < epsilon:
            requested = lr.rng.choice(allowed)
            source, reason = DecisionSource.Q_EXPLORATION, DecisionReason.EXPLORATION
        else:
            requested, source, reason = greedy, DecisionSource.Q_GREEDY, greedy_reason

        return Decision(
            decision_id=decision_id,
            observation_id=o.observation_id,
            time=o.time,
            requested=requested,
            current_green=green,
            source=source,
            reason=reason,
            scores=dict(o.scores),
            counts=dict(o.counts_by_direction),
            tie=tie,
            both_at_cap=False,
            elapsed_green_s=o.elapsed_green_s,
            green_service_limit_s=context.green_service_limit_s,
            epsilon=epsilon,
            exploration_draw=draw,
            **evidence,
        )
