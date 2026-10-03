"""Fixed-time controller (Module 8 baseline, build contract Section J).

At a free five-second choice it retains the current green until the green has
lasted at least the fixed target, then requests the other direction. It never
reads queue scores; they are recorded only as context. The target must be
strictly below the green-service limit so that its own switches happen at a
policy check and are attributed to `fixed_schedule`, never to the forced
service-limit operating rule. Realized greens run up to one policy interval
past the target because the check grid is fixed.
"""

from __future__ import annotations

from traffic_cps.contracts import Decision, DecisionContext, DecisionReason, DecisionSource


class FixedTimeController:
    name = "fixed"

    def __init__(self, green_target_s: int):
        if isinstance(green_target_s, bool) or not isinstance(green_target_s, int) or green_target_s <= 0:
            raise ValueError(f"fixed green target must be a positive integer, got {green_target_s!r}")
        self.green_target_s = green_target_s

    def select(self, context: DecisionContext, decision_id: str) -> Decision:
        o = context.observation
        green = o.current_green
        if green is None or o.elapsed_green_s is None or o.elapsed_green_s < context.minimum_green_s:
            raise ValueError(f"fixed-time controller asked to decide at t={o.time} without a free choice")
        if self.green_target_s >= context.green_service_limit_s:
            raise ValueError(f"fixed green target {self.green_target_s}s must be strictly below the "
                             f"{context.green_service_limit_s}s green-service limit")

        if o.elapsed_green_s >= self.green_target_s:
            requested, reason = green.other, DecisionReason.FIXED_TARGET_REACHED
        else:
            requested, reason = green, DecisionReason.FIXED_TARGET_NOT_REACHED

        return Decision(
            decision_id=decision_id,
            observation_id=o.observation_id,
            time=o.time,
            requested=requested,
            current_green=green,
            source=DecisionSource.FIXED_SCHEDULE,
            reason=reason,
            scores=dict(o.scores),
            counts=dict(o.counts_by_direction),
            tie=False,
            both_at_cap=False,
            elapsed_green_s=o.elapsed_green_s,
            green_service_limit_s=context.green_service_limit_s,
            epsilon=None,
            policy_revision=None,
            fixed_green_target_s=self.green_target_s,
        )
