"""Traffic-light actuator (Module 3).

Validates a controller decision and submits it to the adapter. It never
selects a direction, advances time, or declares a transition complete:
submission only means the request reached the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from traffic_cps.contracts import (
    Decision,
    Direction,
    Observation,
    Origin,
    OriginKind,
    SignalPhase,
    SubmissionResult,
)


class ActuatorError(RuntimeError):
    """Invalid, missing, or unmapped request. Nothing was submitted; the run must stop."""


@dataclass(frozen=True)
class Submission:
    decision_id: str
    target_mask: str
    result: SubmissionResult


class TrafficActuator:
    def __init__(self, adapter: Any):
        self.adapter = adapter

    def submit(self, decision: Decision | None, observation: Observation) -> Submission:
        if decision is None:
            raise ActuatorError("missing decision")
        if not isinstance(decision.requested, Direction):
            raise ActuatorError(f"decision {decision.decision_id}: invalid requested direction "
                                f"{decision.requested!r}")
        if decision.observation_id != observation.observation_id or decision.time != observation.time:
            raise ActuatorError(f"decision {decision.decision_id} refers to {decision.observation_id} at "
                                f"t={decision.time}, not {observation.observation_id} at t={observation.time}")
        try:
            target_mask = self.adapter.signal_map.mask(SignalPhase.green(decision.requested))
        except Exception as exc:
            raise ActuatorError(f"no validated signal mapping for {decision.requested.value}: {exc}") from exc
        origin = Origin(OriginKind.CONTROLLER_DECISION, decision.decision_id)
        result = self.adapter.request(decision.requested, origin, observation)
        return Submission(decision.decision_id, target_mask, result)
