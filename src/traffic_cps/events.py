"""Event log: JSONL records with a fixed envelope (build contract, Section F).

Each line is one complete, strict JSON object (no NaN/Infinity) followed by a
newline. Records are evidence, kept separate from later explanations. Run
directories and event logs are created exclusively and never overwritten.
Payload builders live here so the runtime and any reader share one schema.
"""

from __future__ import annotations

import dataclasses
import json
import os
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from traffic_cps.contracts import (
    Decision,
    HoldReason,
    Observation,
    SafetyOverride,
    SafetyViolation,
    SignalEvent,
    SignalTiming,
    SubmissionResult,
)

SCHEMA_VERSION = 1
EVENTS_FILE = "events.jsonl"
RUN_MANIFEST = "run.manifest.json"
CONFIG_RESOLVED = "config.resolved.json"
SCENARIO_MANIFEST_COPY = "scenario.manifest.json"
POLICY_START = "policy_start.json"
SUMO_LOG = "sumo.log"
REPORT_FILE = "report.html"

EVENT_TYPES = frozenset({
    "run_start", "observation", "decision", "submission", "signal_change", "constraint_hold",
    "operating_action", "safety_override", "safety_violation", "learning_update",
    "feedback_discarded", "phantom_demand", "masked_demand", "checkpoint_saved", "error", "run_end",
})


class EventError(RuntimeError):
    """Event could not be serialized or written, or a path would be overwritten."""


def to_jsonable(value: Any) -> Any:
    """Records, enums, and mappings to plain JSON types (enum keys become their values)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {(k.value if isinstance(k, Enum) else str(k)): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Path):
        return value.as_posix()
    return value


def dumps_strict(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise EventError(f"not strict JSON: {exc}") from exc


def write_json_exclusive(path: Path, data: Any) -> None:
    """Write a JSON artifact that must not already exist."""
    text = json.dumps(to_jsonable(data), indent=2, sort_keys=True, allow_nan=False) + "\n"
    try:
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(text)
    except FileExistsError as exc:
        raise EventError(f"{path} already exists; refusing to overwrite") from exc


def create_run_dir(output_root: Path | str, run_id: str) -> Path:
    path = Path(output_root) / run_id
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise EventError(f"run directory {path} already exists; refusing to overwrite") from exc
    return path


class EventWriter:
    def __init__(self, path: Path | str, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.seq = 0
        self._ids: set[str] = set()
        try:
            self._fh = open(self.path, "x", encoding="utf-8")
        except FileExistsError as exc:
            raise EventError(f"{self.path} already exists; refusing to overwrite") from exc

    def append(self, event_type: str, sim_time_s: int, payload: Mapping[str, Any],
               event_id: str | None = None) -> str:
        """Serialize fully before writing, so a failure never leaves a partial line."""
        if event_type not in EVENT_TYPES:
            raise EventError(f"unknown event type {event_type!r}")
        if isinstance(sim_time_s, bool) or not isinstance(sim_time_s, int):
            raise EventError(f"sim_time_s must be an integer, got {sim_time_s!r}")
        seq = self.seq + 1
        event_id = event_id or f"{event_type}-{seq}"
        if event_id in self._ids:
            raise EventError(f"duplicate event id {event_id}")
        line = dumps_strict({
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "event_id": event_id,
            "seq": seq,
            "sim_time_s": sim_time_s,
            "type": event_type,
            "payload": to_jsonable(payload),
        })
        self._fh.write(line + "\n")
        self.seq = seq
        self._ids.add(event_id)
        return event_id

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()


# ----------------------------------------------------------------------
# Payload builders (one schema shared by writer and readers)
# ----------------------------------------------------------------------

def _origin(origin) -> dict:
    return {"kind": origin.kind.value, "id": origin.origin_id}


def observation_payload(o: Observation) -> dict:
    return {
        "observation_id": o.observation_id,
        "counts_by_lane": o.counts_by_lane,
        "counts_by_direction": o.counts_by_direction,
        "lane_scores": o.lane_scores,
        "scores": o.scores,
        "colors": o.colors,
        "color_since_s": o.color_since_s,
        "phase": o.phase,
        "current_green": o.current_green,
        "elapsed_green_s": o.elapsed_green_s,
        "transition_target": o.transition_target,
        "transition_start_s": o.transition_start_s,
        "raw_signal": o.raw_signal,
        "measurement": o.measurement,
    }


def decision_payload(d: Decision) -> dict:
    return {
        "decision_id": d.decision_id,
        "observation_id": d.observation_id,
        "requested": d.requested,
        "current_green": d.current_green,
        "retains": d.retains,
        "source": d.source,
        "reason": d.reason,
        "scores": d.scores,
        "counts": d.counts,
        "tie": d.tie,
        "both_at_cap": d.both_at_cap,
        "elapsed_green_s": d.elapsed_green_s,
        "green_service_limit_s": d.green_service_limit_s,
        "epsilon": d.epsilon,
        "policy_revision": d.policy_revision,
        "fixed_green_target_s": d.fixed_green_target_s,
    }


def submission_payload(result: SubmissionResult, target_mask: str | None) -> dict:
    return {
        "origin": _origin(result.origin),
        "target": result.target,
        "target_mask": target_mask,
        "status": result.status,
        "block_reason": result.block_reason,
        "error": result.error,
        "signal_event_id": result.signal_event.event_id if result.signal_event else None,
        "override_invariant": result.override.invariant if result.override else None,
        "violations": [v.invariant for v in result.violations],
    }


def unexpected_block_payload(result: SubmissionResult) -> dict:
    """An `error` diagnostic: the timing gate allowed a request that the adapter still blocked."""
    return {
        "stage": "unexpected_block",
        "recovered": True,
        "origin": _origin(result.origin),
        "target": result.target,
        "block_reason": result.block_reason,
        "message": "request allowed by the timing gate was blocked; discarded, not queued",
    }


def signal_change_payload(e: SignalEvent) -> dict:
    return {
        "signal_event_id": e.event_id,
        "old_mask": e.old_mask,
        "new_mask": e.new_mask,
        "old_colors": e.old_colors,
        "new_colors": e.new_colors,
        "origin": _origin(e.origin),
        "transition_target": e.transition_target,
        "monitor": {"allowed": e.monitor_result.allowed, "invariant": e.monitor_result.invariant,
                    "reason": e.monitor_result.reason},
    }


def constraint_hold_payload(reason: HoldReason, timing: SignalTiming, minimum_green_s: int) -> dict:
    return {
        "reason": reason,
        "phase": timing.phase,
        "current_green": timing.current_green,
        "elapsed_green_s": timing.elapsed_green_s,
        "minimum_green_s": minimum_green_s,
        "transition_target": timing.transition_target,
        "transition_start_s": timing.transition_start_s,
    }


def operating_action_payload(o: Observation, timing: SignalTiming, limit_s: int) -> dict:
    green = timing.current_green
    return {
        "reason": "service_limit_competing_demand",
        "observation_id": o.observation_id,
        "green": green,
        "requested": green.other,
        "elapsed_green_s": timing.elapsed_green_s,
        "green_service_limit_s": limit_s,
        "competing_measured_count": o.counts_by_direction[green.other],
        "counts": o.counts_by_direction,
        "scores": o.scores,
    }


def safety_override_payload(o: SafetyOverride) -> dict:
    return {"proposed_mask": o.proposed_mask, "current_mask": o.current_mask, "origin": _origin(o.origin),
            "invariant": o.invariant, "reason": o.reason}


def safety_violation_payload(v: SafetyViolation) -> dict:
    return {"invariant": v.invariant, "raw_signal": v.raw_signal, "previous_raw_signal": v.previous_raw_signal,
            "stage": v.stage, "detail": v.detail}
