"""Queue-based controller (Module 2A).

Requests the direction with the higher queue score; on equal scores (absolute
tolerance) it retains the current green. It compares incoming queue scores
only, so it is queue-based, not max-pressure. Priorities 1 and 2 of the
operating table (transition in progress, minimum green) are timing holds
enforced by the runtime before this controller is asked; priority 3 (service
limit with competing demand) is handled here when it applies at a free choice.
"""

from __future__ import annotations

from traffic_cps.contracts import Decision, DecisionContext, DecisionReason, DecisionSource, Direction


def queue_choice(scores, counts, green: Direction, elapsed_s: float, service_limit_s: float, tol: float,
                 cap: float) -> tuple[Direction, DecisionReason, bool, bool]:
    """The queue rule as a pure function: (requested, reason, tie, both_at_cap).

    Shared by the controller and by the explainer's fidelity check, so both apply one rule.
    """
    other = green.other
    own, competing = scores[green], scores[other]
    tie = abs(own - competing) <= tol
    both_at_cap = tie and own >= cap - tol and competing >= cap - tol
    if elapsed_s >= service_limit_s and counts[other] > 0:
        return other, DecisionReason.SERVICE_LIMIT_COMPETING_DEMAND, tie, both_at_cap
    if tie:
        return green, DecisionReason.EQUAL_QUEUE_SCORES, tie, both_at_cap
    return (green if own > competing else other), DecisionReason.HIGHER_QUEUE_SCORE, tie, both_at_cap


class QueueController:
    name = "queue"

    def select(self, context: DecisionContext, decision_id: str) -> Decision:
        o = context.observation
        green = o.current_green
        if green is None or o.elapsed_green_s is None or o.elapsed_green_s < context.minimum_green_s:
            raise ValueError(f"queue controller asked to decide at t={o.time} without a free choice")
        requested, reason, tie, both_at_cap = queue_choice(
            o.scores, o.counts_by_direction, green, o.elapsed_green_s, context.green_service_limit_s,
            context.tie_abs_tolerance, context.score_cap)

        return Decision(
            decision_id=decision_id,
            observation_id=o.observation_id,
            time=o.time,
            requested=requested,
            current_green=green,
            source=DecisionSource.QUEUE_RULE,
            reason=reason,
            scores=dict(o.scores),
            counts=dict(o.counts_by_direction),
            tie=tie,
            both_at_cap=both_at_cap,
            elapsed_green_s=o.elapsed_green_s,
            green_service_limit_s=context.green_service_limit_s,
            epsilon=None,
            policy_revision=None,
        )
