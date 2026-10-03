"""Queue-based controller (Module 2A).

Requests the direction with the higher queue score; on equal scores (absolute
tolerance) it retains the current green. It compares incoming queue scores
only, so it is queue-based, not max-pressure. Priorities 1 and 2 of the
operating table (transition in progress, minimum green) are timing holds
enforced by the runtime before this controller is asked; priority 3 (service
limit with competing demand) is handled here when it applies at a free choice.
"""

from __future__ import annotations

from traffic_cps.contracts import Decision, DecisionContext, DecisionReason, DecisionSource


class QueueController:
    name = "queue"

    def select(self, context: DecisionContext, decision_id: str) -> Decision:
        o = context.observation
        green = o.current_green
        if green is None or o.elapsed_green_s is None or o.elapsed_green_s < context.minimum_green_s:
            raise ValueError(f"queue controller asked to decide at t={o.time} without a free choice")

        other = green.other
        own, competing = o.scores[green], o.scores[other]
        tol = context.tie_abs_tolerance
        tie = abs(own - competing) <= tol
        cap = context.score_cap
        both_at_cap = tie and own >= cap - tol and competing >= cap - tol

        if o.elapsed_green_s >= context.green_service_limit_s and o.counts_by_direction[other] > 0:
            requested, reason = other, DecisionReason.SERVICE_LIMIT_COMPETING_DEMAND
        elif tie:
            requested, reason = green, DecisionReason.EQUAL_QUEUE_SCORES
        else:
            requested = green if own > competing else other
            reason = DecisionReason.HIGHER_QUEUE_SCORE

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
