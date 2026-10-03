"""Runtime: the sole run coordinator (Module 5, build contract Section E).

The runtime owns the order of operations and the clock; each component owns
its own work. Per second t = 1..H:

  1. advance one second; read raw state (same-second retries); monitor S1-S3; confirm
  2. (t < H) complete a yellow that has lasted yellow_s
  3. build and log the primary observation for t
  5. (t < H) enforce the green-service limit as an operating action; check S4
  6. (t < H, t % interval == 0) timing hold, or controller decision -> actuator
  7. flush the tick

(Step 4, closing learning feedback at H, arrives with Q-learning in chunk 4.)
Any failure stops the run cleanly: completed records are kept and the run is
marked incomplete. Nothing continues after a safety violation.
"""

from __future__ import annotations

import importlib.metadata
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from traffic_cps import SUMO_VERSION, __version__
from traffic_cps import events as ev
from traffic_cps.actuator import TrafficActuator
from traffic_cps.adapter import SumoAdapter
from traffic_cps.config import Config, config_digest, sha256_file
from traffic_cps.contracts import (
    CompletionStatus,
    DecisionContext,
    Direction,
    HoldReason,
    Mode,
    Observation,
    Origin,
    OriginKind,
    RunResult,
    SignalPhase,
    SubmissionResult,
    SubmissionStatus,
)
from traffic_cps.controllers import Controller
from traffic_cps.safety import SafetyMonitor
from traffic_cps.scenario import SCENARIO_MANIFEST
from traffic_cps.sensor import SensorFailure, TrafficSensor

SCORE_CAP = 2.0  # two lanes per direction, each lane score capped at 1


class RunStop(RuntimeError):
    """A condition that ends the run as incomplete (already logged where possible)."""


def hold_reason(state, minimum_green_s: int) -> HoldReason | None:
    """Timing gate for a scheduled control point (works on an Observation or SignalTiming)."""
    if state.transition_target is not None:
        return HoldReason.TRANSITION_IN_PROGRESS
    if state.elapsed_green_s is None or state.elapsed_green_s < minimum_green_s:
        return HoldReason.MINIMUM_GREEN_NOT_REACHED
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Runtime:
    def __init__(
        self,
        config: Config,
        scenario_dir: Path | str,
        controller: Controller,
        output_dir: Path | str,
        horizon_s: int | None = None,
        mode: Mode = Mode.EVALUATE,
        notify: Callable[[str], None] | None = None,
        run_id: str | None = None,
    ):
        self.config = config
        self.scenario_dir = Path(scenario_dir)
        self.controller = controller
        self.output_dir = Path(output_dir)
        self.horizon_s = config.simulation.horizon_s if horizon_s is None else horizon_s
        self.mode = mode
        self.notify = notify
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = run_id or f"{controller.name}-{stamp}-{uuid.uuid4().hex[:6]}"
        self.counters = {k: 0 for k in (
            "ticks", "observations", "scheduled_checks", "constraint_holds", "eligible_choices", "decisions",
            "accepted_requests", "switches_requested", "blocked_requests", "operating_actions",
            "signal_changes", "safety_overrides", "safety_violations", "retrieval_retries",
        )}
        self.final_valid_time: int | None = None
        self.trace_ok = True

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def run(self) -> RunResult:
        self.run_dir = ev.create_run_dir(self.output_dir / "runs", self.run_id)
        self._write_artifacts()
        self.writer = ev.EventWriter(self.run_dir / ev.EVENTS_FILE, self.run_id)
        self.writer.append("run_start", 0, {
            "controller": self.controller.name,
            "mode": self.mode,
            "horizon_s": self.horizon_s,
            "artifacts": {name: sha256_file(self.run_dir / name) for name in (
                ev.CONFIG_RESOLVED, ev.SCENARIO_MANIFEST_COPY, ev.RUN_MANIFEST, ev.POLICY_START)},
        })

        cfg = self.config
        self.monitor = SafetyMonitor(cfg.signal.yellow_s, cfg.signal.minimum_green_s,
                                     cfg.signal.green_service_limit_s, cfg.simulation.initial_green)
        self.adapter = SumoAdapter(cfg, self.monitor, sumo_log=self.run_dir / ev.SUMO_LOG)
        self.actuator = TrafficActuator(self.adapter)

        completion, failure = CompletionStatus.INCOMPLETE, None
        try:
            self._start()
            while self.adapter.time < self.horizon_s:
                self._tick()
            completion = CompletionStatus.COMPLETED
        except Exception as exc:  # every failure ends the run explicitly; nothing is swallowed
            failure = f"{type(exc).__name__}: {exc}"
            self._log_error("run", exc)
        finally:
            self.adapter.close()

        persistence_ok = True
        try:
            self.writer.append("run_end", self.adapter.time or 0, {
                "completion": completion,
                "failure": failure,
                "planned_horizon_s": self.horizon_s,
                "final_valid_time_s": self.final_valid_time,
                "counters": self.counters,
                "finished_at_utc": _utc_now(),
            })
            self.writer.close()
        except Exception as exc:
            persistence_ok = False
            failure = failure or f"event log close failed: {exc}"

        return RunResult(
            run_id=self.run_id, run_dir=str(self.run_dir), completion=completion,
            trace_complete=persistence_ok and self.trace_ok,
            persistence_ok=persistence_ok, planned_horizon_s=self.horizon_s,
            final_valid_time_s=self.final_valid_time, failure=failure, counters=dict(self.counters),
        )

    def _write_artifacts(self) -> None:
        resolved = self.config.to_dict()
        resolved["overrides"] = {"horizon_s": self.horizon_s}
        resolved["config_sha256"] = config_digest(self.config)
        ev.write_json_exclusive(self.run_dir / ev.CONFIG_RESOLVED, resolved)
        shutil.copyfile(self.scenario_dir / SCENARIO_MANIFEST, self.run_dir / ev.SCENARIO_MANIFEST_COPY)
        ev.write_json_exclusive(self.run_dir / ev.RUN_MANIFEST, {
            "run_id": self.run_id,
            "controller": self.controller.name,
            "mode": self.mode,
            "horizon_s": self.horizon_s,
            "scenario_dir": str(self.scenario_dir),
            "scenario_manifest_sha256": sha256_file(self.scenario_dir / SCENARIO_MANIFEST),
            "config_sha256": config_digest(self.config),
            "versions": {
                "traffic_cps": __version__,
                "python": sys.version.split()[0],
                "sumo_pinned": SUMO_VERSION,
                "traci": importlib.metadata.version("traci"),
                "sumolib": importlib.metadata.version("sumolib"),
            },
            "started_at_utc": _utc_now(),
        })
        ev.write_json_exclusive(self.run_dir / ev.POLICY_START, None)  # baselines have no Q-table

    def _start(self) -> None:
        cfg = self.config
        self.adapter.start(self.scenario_dir, horizon_s=self.horizon_s)
        self.sensor = TrafficSensor(cfg.network.lanes_by_direction(), self.adapter.lane_lengths,
                                    cfg.sensor.vehicle_length_m, cfg.sensor.minimum_gap_m, cfg.sensor.retries)
        self.writer.append("signal_change", 0, ev.signal_change_payload(self.adapter.initial_event))
        self.counters["signal_changes"] += 1
        raw, quality = self.sensor.read(self.adapter.read_raw, 0, self.notify)
        self._log_retries(0, quality)
        obs = self.sensor.observe(raw, self.adapter.timing(), quality)
        self._log_observation(obs)
        self._control_point(obs)  # t=0: skipped for minimum green, never time-advanced
        self.writer.flush()

    # ------------------------------------------------------------------
    # One second
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        H = self.horizon_s
        t = self.adapter.advance_one_second()                               # step 1
        self.counters["ticks"] += 1
        raw, quality = self.sensor.read(self.adapter.read_raw, t, self.notify)
        self._log_retries(t, quality)
        self._log_violations(self.monitor.observe(raw.signal_state, t, "tick"))
        self.adapter.confirm(raw)

        if t < H:                                                           # step 2
            done = self.adapter.complete_due_transition()
            if done is not None:
                self._log_result(done, target_mask=None, log_submission=False)

        obs = self.sensor.observe(raw, self.adapter.timing(), quality)      # step 3
        self._log_observation(obs)

        if t < H:                                                           # step 5
            timing = self.adapter.timing()
            if self.adapter.service_limit_reached() and obs.counts_by_direction[timing.current_green.other] > 0:
                op_id = self.writer.append("operating_action", t, ev.operating_action_payload(
                    obs, timing, self.config.signal.green_service_limit_s), event_id=f"op-{t}")
                self.counters["operating_actions"] += 1
                target = timing.current_green.other
                result = self.adapter.request(target, Origin(OriginKind.OPERATING_ACTION, op_id), obs)
                self._log_result(result, self.adapter.signal_map.mask(SignalPhase.green(target)))
            self._log_violations(self.monitor.check_service_deadline(obs))

            if t % self.config.simulation.control_interval_s == 0:         # step 6
                self._control_point(obs)

        self.writer.flush()                                                 # step 7

    def _control_point(self, obs: Observation) -> None:
        cfg = self.config
        t = obs.time
        self.counters["scheduled_checks"] += 1
        timing = self.adapter.timing()
        reason = hold_reason(timing, cfg.signal.minimum_green_s)
        if reason is not None:
            self.writer.append("constraint_hold", t, ev.constraint_hold_payload(
                reason, timing, cfg.signal.minimum_green_s))
            self.counters["constraint_holds"] += 1
            return

        self.counters["eligible_choices"] += 1
        context = DecisionContext(
            observation=obs, allowed=(Direction.NS, Direction.WE), mode=self.mode,
            minimum_green_s=cfg.signal.minimum_green_s, green_service_limit_s=cfg.signal.green_service_limit_s,
            tie_abs_tolerance=cfg.learning.tie_abs_tolerance, score_cap=SCORE_CAP,
        )
        decision = self.controller.select(context, f"dec-{t}")
        self.writer.append("decision", t, ev.decision_payload(decision), event_id=decision.decision_id)
        self.counters["decisions"] += 1
        submission = self.actuator.submit(decision, obs)
        self._log_result(submission.result, submission.target_mask)
        if submission.result.status is SubmissionStatus.ACCEPTED:
            self.counters["accepted_requests"] += 1
            if not decision.retains:
                self.counters["switches_requested"] += 1

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log_observation(self, obs: Observation) -> None:
        self.writer.append("observation", obs.time, ev.observation_payload(obs), event_id=obs.observation_id)
        self.counters["observations"] += 1
        self.final_valid_time = obs.time

    def _log_retries(self, t: int, quality) -> None:
        for failure in quality.failures:
            self.writer.append("error", t, {"stage": "sensor_retrieval", "recovered": True,
                                            "attempt": failure.attempt, "field": failure.field,
                                            "error": failure.error})
            self.counters["retrieval_retries"] += 1

    def _log_result(self, result: SubmissionResult, target_mask: str | None, log_submission: bool = True) -> None:
        t = result.time
        if log_submission:
            self.writer.append("submission", t, ev.submission_payload(result, target_mask))
        if result.signal_event is not None:
            self.writer.append("signal_change", t, ev.signal_change_payload(result.signal_event))
            self.counters["signal_changes"] += 1
        if result.override is not None:
            self.writer.append("safety_override", t, ev.safety_override_payload(result.override))
            self.counters["safety_overrides"] += 1
        if result.status is SubmissionStatus.BLOCKED:
            self.counters["blocked_requests"] += 1
        self._log_violations(result.violations)
        if result.status in (SubmissionStatus.FAILED, SubmissionStatus.UNCONFIRMED):
            raise RunStop(f"signal command {result.status.value} at t={t}: {result.error}")

    def _log_violations(self, violations) -> None:
        for v in violations:
            self.writer.append("safety_violation", v.time, ev.safety_violation_payload(v))
            self.counters["safety_violations"] += 1
        if violations:
            raise RunStop(f"safety violation {[v.invariant for v in violations]} at t={violations[0].time}")

    def _log_error(self, stage: str, exc: Exception) -> None:
        payload = {"stage": stage, "recovered": False, "kind": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, SensorFailure):
            payload["failures"] = ev.to_jsonable(exc.failures)
        try:
            self.writer.append("error", self.adapter.time if self.adapter.time is not None else 0, payload)
        except Exception:
            self.trace_ok = False  # the error itself could not be recorded; reported in RunResult
