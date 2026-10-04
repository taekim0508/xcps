"""Runtime: the sole run coordinator (Module 5, build contract Section E).

The runtime owns the order of operations and the clock; each component owns
its own work. Per second t = 1..H:

  1. advance one second; read raw state (same-second retries); monitor S1-S3; confirm
  2. (t < H) complete a yellow that has lasted yellow_s
  3. build and log the primary observation for t; add its reward once to pending feedback
  4. (t = H) close pending feedback if a fresh choice would be allowed, else discard it
  5. (t < H) enforce the green-service limit as an operating action; check S4
  6. (t < H, t % interval == 0) timing hold, or: close pending feedback, then controller
     decision -> actuator; an accepted training choice opens new feedback
  7. flush the tick

Learning happens only in training mode. Feedback for an accepted choice starts
with the sample at t+1, survives forced switches, and closes at the next
scheduled point that allows a fresh choice, where the update is applied before
the next selection. Blocked requests open nothing. A failure discards the
unfinished window; completed updates stay. `train` runs episodes back to back
with one Learner, so table and exploration progress carry over and pending
feedback never does.

Any failure stops the run cleanly: completed records are kept and the run is
marked incomplete. Nothing continues after a safety violation. A request the
timing gate allowed but the adapter still blocked is logged as an integration
diagnostic and discarded, never queued for a later second.
"""

from __future__ import annotations

import importlib.metadata
import json
import shutil
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from traffic_cps import SUMO_VERSION, __version__
from traffic_cps import events as ev
from traffic_cps.actuator import TrafficActuator
from traffic_cps.adapter import SumoAdapter
from traffic_cps.checkpoints import CheckpointError
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
from traffic_cps.faults import service_demand_fault
from traffic_cps.learning import Learner, add_sample, open_interval
from traffic_cps.safety import SafetyMonitor
from traffic_cps.scenario import SCENARIO_MANIFEST
from traffic_cps.sensor import SensorFailure, TrafficSensor

SCORE_CAP = 2.0  # two lanes per direction, each lane score capped at 1


class RunStop(RuntimeError):
    """A condition that ends the run as incomplete (already logged where possible)."""


class StopRequest:
    """Set by an interrupt handler; the runtime honors it only at the end of a simulated second,
    after that second's records are flushed, so no update or snapshot is half-applied."""

    def __init__(self) -> None:
        self.requested = False
        self.reason: str | None = None

    def request(self, reason: str = "interrupt requested") -> None:
        self.requested, self.reason = True, reason


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
        connection_wrapper: Callable[[Any], Any] | None = None,
        learner: Learner | None = None,
        session: "TrainingSession | None" = None,
        stop: StopRequest | None = None,
        provenance: dict | None = None,
        measurement: Any = None,
    ):
        if mode is Mode.TRAIN and learner is None:
            raise ValueError("training mode requires a Learner")
        self.config = config
        self.scenario_dir = Path(scenario_dir)
        self.controller = controller
        self.output_dir = Path(output_dir)
        self.horizon_s = config.simulation.horizon_s if horizon_s is None else horizon_s
        self.mode = mode
        self.notify = notify
        self.connection_wrapper = connection_wrapper  # fault injection in tests only
        self.learner = learner
        self.session = session          # training checkpoints; None for evaluation and baselines
        self.stop = stop
        self.provenance = provenance    # traffic seed, attempt, learning source, checkpoint identity
        self.measurement = measurement  # robustness experiment only: faults.SensorNoise
        self.departed: set[str] = set()
        self.arrived: set[str] = set()
        self.checkpoint_status: str | None = None
        self.pending = None  # PendingFeedback for the last accepted training choice; never crosses episodes
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = run_id or f"{controller.name}-{stamp}-{uuid.uuid4().hex[:6]}"
        self.counters = {k: 0 for k in (
            "ticks", "observations", "scheduled_checks", "constraint_holds", "eligible_choices", "decisions",
            "accepted_requests", "switches_requested", "blocked_requests", "unexpected_blocks", "operating_actions",
            "signal_changes", "safety_overrides", "safety_violations", "retrieval_retries",
            "learning_updates", "feedback_discarded", "phantom_demand", "masked_demand",
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
        self.adapter = SumoAdapter(cfg, self.monitor, connection_wrapper=self.connection_wrapper,
                                   sumo_log=self.run_dir / ev.SUMO_LOG)
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
            stopped = self.stop is not None and self.stop.requested
            self._discard("interrupted" if stopped else "run_failed",
                          self.adapter.time if self.adapter.time is not None else 0)
        finally:
            self.adapter.close()

        if self.session is not None:
            failure = self._final_checkpoint(completion, failure)

        persistence_ok = self.writer.failed is None
        if persistence_ok:
            try:
                self.writer.append("run_end", self.adapter.time or 0, {
                    "completion": completion,
                    "failure": failure,
                    "planned_horizon_s": self.horizon_s,
                    "final_valid_time_s": self.final_valid_time,
                    "counters": self.counters,
                    "learning": self._learning_summary(),
                    "checkpoint_status": self.checkpoint_status,
                    "vehicle_accounting": self._vehicle_accounting(completion),
                    "finished_at_utc": _utc_now(),
                })
            except Exception as exc:
                persistence_ok = False
                failure = failure or f"event log write failed: {exc}"
        try:
            self.writer.close()
        except Exception as exc:
            persistence_ok = False
            failure = failure or f"event log close failed: {exc}"
        persistence_ok = persistence_ok and self.writer.failed is None
        if not persistence_ok:
            self._say(f"event log {self.run_dir / ev.EVENTS_FILE} failed ({self.writer.failed or failure}); "
                      f"the trace is incomplete")

        return RunResult(
            run_id=self.run_id, run_dir=str(self.run_dir), completion=completion,
            trace_complete=persistence_ok and self.trace_ok,
            persistence_ok=persistence_ok, planned_horizon_s=self.horizon_s,
            final_valid_time_s=self.final_valid_time, failure=failure, counters=dict(self.counters),
            checkpoint_status=self.checkpoint_status,
            last_checkpoint_id=self.session.last_checkpoint_id if self.session else None,
        )

    def _vehicle_accounting(self, completion: CompletionStatus) -> dict:
        """Classify every scheduled vehicle exactly once at the end of the run (Section H)."""
        try:
            manifest = json.loads((self.scenario_dir / SCENARIO_MANIFEST).read_text(encoding="utf-8"))
            scheduled = [v["vehicle_id"] for v in manifest["demand"]["scheduled_vehicles"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"status": "unavailable", "reason": f"scheduled vehicles unreadable: {exc}"}
        scheduled_set = set(scheduled)
        arrived = self.arrived
        active = self.departed - self.arrived
        not_inserted = scheduled_set - self.departed
        unexpected = (self.departed | self.arrived) - scheduled_set
        consistent = not unexpected and arrived <= self.departed \
            and len(arrived) + len(active) + len(not_inserted) == len(scheduled_set) == len(scheduled)
        return {
            "status": "complete" if completion is CompletionStatus.COMPLETED else "partial",
            "consistent": consistent,
            "scheduled": len(scheduled),
            "arrived": len(arrived),
            "active": len(active),
            "not_inserted": len(not_inserted),
            "arrived_ids": sorted(arrived),
            "active_ids": sorted(active),
            "not_inserted_ids": sorted(not_inserted),
            "unexpected_ids": sorted(unexpected),
            "final_valid_time_s": self.final_valid_time,
        }

    def _say(self, message: str) -> None:
        """Report outside the event log (the log may be the thing that failed)."""
        if self.notify is not None:
            self.notify(message)

    # ------------------------------------------------------------------
    # Checkpoints (training only)
    # ------------------------------------------------------------------

    def _checkpoint(self, reason: str, t: int) -> None:
        """Save now; log checkpoint_saved if the log works. Raises CheckpointWriteError."""
        from traffic_cps.checkpoints import CheckpointWriteError

        try:
            result = self.session.save(reason, self.run_id, t)
        except CheckpointWriteError as exc:
            self.checkpoint_status = exc.status
            if self.writer.failed is None:
                try:
                    self.writer.append("error", t, {"stage": "checkpoint_write", "recovered": False,
                                                    "reason": reason, "status": exc.status, "message": str(exc),
                                                    "last_saved_checkpoint": self.session.last_checkpoint_id})
                    self.writer.flush()
                except Exception:
                    self.trace_ok = False
            raise
        self.checkpoint_status = "saved"
        if self.writer.failed is None:
            self.writer.append("checkpoint_saved", t, ev.checkpoint_saved_payload(
                result, reason, self.session.progress_summary()))
            self.writer.flush()

    def _final_checkpoint(self, completion: CompletionStatus, failure: str | None) -> str | None:
        """Episode-end save after a completed run, or one shutdown save of completed learning after a
        stop. Never retried against a destination that already failed. Returns the updated failure text."""
        completed = completion is CompletionStatus.COMPLETED
        reason = "episode_end" if completed else "shutdown"
        t = self.adapter.time if self.adapter.time is not None else 0
        if self.session.failed is not None:
            self.checkpoint_status = "failed"
            self._say(f"shutdown checkpoint not attempted: the checkpoint destination already failed "
                      f"({self.session.failed}); last saved checkpoint: {self.session.last_checkpoint_id or 'none'}")
            return failure
        if completed:
            self.session.episode_completed()
        try:
            self._checkpoint(reason, t)
        except Exception as exc:
            last = self.session.last_checkpoint_id or "none (no saved training state to restore)"
            self._say(f"{reason} checkpoint failed: {exc}; last saved checkpoint: {last}")
            if completed:
                return f"simulation completed, saving failed: {exc}"
            return f"{failure}; shutdown checkpoint failed: {exc}"
        if not completed:
            self._say(f"shutdown checkpoint saved: {self.session.last_checkpoint_id} (completed learning only; "
                      f"unfinished feedback discarded)")
        return failure

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
            "provenance": self.provenance,
            "measurement": self.measurement.describe() if self.measurement is not None else {"label": "clean"},
            "versions": {
                "traffic_cps": __version__,
                "python": sys.version.split()[0],
                "sumo_pinned": SUMO_VERSION,
                "traci": importlib.metadata.version("traci"),
                "sumolib": importlib.metadata.version("sumolib"),
            },
            "started_at_utc": _utc_now(),
        })
        # Baselines have no Q-table; Q-learning records the full starting table (Section F).
        ev.write_json_exclusive(self.run_dir / ev.POLICY_START,
                                self.learner.snapshot() if self.learner is not None else None)

    def _start(self) -> None:
        cfg = self.config
        self.adapter.start(self.scenario_dir, horizon_s=self.horizon_s)
        self.sensor = TrafficSensor(cfg.network.lanes_by_direction(), self.adapter.lane_lengths,
                                    cfg.sensor.vehicle_length_m, cfg.sensor.minimum_gap_m, cfg.sensor.retries)
        self.writer.append("signal_change", 0, ev.signal_change_payload(self.adapter.initial_event))
        self.counters["signal_changes"] += 1
        raw, quality = self.sensor.read(self.adapter.read_raw, 0, self.notify)
        self._log_retries(0, quality)
        obs = self.sensor.observe(raw, self.adapter.timing(), quality, measure=self.measurement)
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

        obs = self.sensor.observe(raw, self.adapter.timing(), quality,      # step 3
                                  measure=self.measurement)
        self._log_observation(obs)
        if self.pending is not None:
            self.pending = add_sample(self.pending, obs)

        if t == H:                                                          # step 4
            self._close_at_horizon(obs)

        if t < H:                                                           # step 5
            timing = self.adapter.timing()
            limit = self.config.signal.green_service_limit_s
            op_id = None
            forced = self.adapter.service_limit_reached() \
                and obs.counts_by_direction[timing.current_green.other] > 0
            if forced:
                op_id = self.writer.append("operating_action", t, ev.operating_action_payload(
                    obs, timing, limit), event_id=f"op-{t}")
                self.counters["operating_actions"] += 1
            if timing.current_green is not None:                            # truth vs measurement (Section H)
                other = timing.current_green.other
                fault = service_demand_fault(obs.true_counts_by_direction[other], obs.counts_by_direction[other],
                                             timing.elapsed_green_s, limit, forced)
                if fault is not None:
                    self.writer.append(fault, t, ev.demand_fault_payload(fault, obs, timing, limit, op_id))
                    self.counters[fault] += 1
            if forced:
                target = timing.current_green.other
                result = self.adapter.request(target, Origin(OriginKind.OPERATING_ACTION, op_id), obs)
                self._log_result(result, self.adapter.signal_map.mask(SignalPhase.green(target)))
            self._log_violations(self.monitor.check_service_deadline(obs))

            if t % self.config.simulation.control_interval_s == 0:         # step 6
                self._control_point(obs)

        self.writer.flush()                                                 # step 7
        if self.session is not None and self.session.due():
            self._checkpoint("every_n_updates", t)
        if self.stop is not None and self.stop.requested:
            raise RunStop(f"stopped at t={t}: {self.stop.reason}")

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
        if self.pending is not None:  # update before the next selection
            self._close_pending(obs)
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
            if self.mode is Mode.TRAIN:
                if decision.q_state is None:
                    raise RunStop(f"training decision {decision.decision_id} has no Q-state")
                self.pending = open_interval(decision.decision_id, decision.q_state, decision.requested, t)
                self.learner.schedule.accepted()

    # ------------------------------------------------------------------
    # Learning feedback (training only)
    # ------------------------------------------------------------------

    def _close_pending(self, obs: Observation) -> None:
        update = self.learner.close(self.pending, self.learner.encode(obs), obs.time)
        self.pending = None
        self.writer.append("learning_update", obs.time, ev.learning_update_payload(update),
                           event_id=f"upd-{update.policy_revision}")
        self.counters["learning_updates"] += 1

    def _close_at_horizon(self, obs: Observation) -> None:
        """Section E step 4: no command; update only if this boundary would allow a fresh choice."""
        if self.pending is None:
            return
        timing = self.adapter.timing()
        reason = hold_reason(timing, self.config.signal.minimum_green_s)
        if reason is None and self.adapter.service_limit_reached() \
                and obs.counts_by_direction[timing.current_green.other] > 0:
            reason = "service_limit_due"  # a due forced switch is not an eligible bootstrap boundary
        if reason is None:
            self._close_pending(obs)
        else:
            self._discard(reason.value if isinstance(reason, HoldReason) else reason, obs.time)

    def _discard(self, reason: str, t: int) -> None:
        """Drop unfinished feedback from learning; record it when the log still works."""
        if self.pending is None:
            return
        pending, self.pending = self.pending, None
        self.counters["feedback_discarded"] += 1
        if self.writer.failed is None:
            try:
                self.writer.append("feedback_discarded", t, ev.feedback_discarded_payload(pending, reason, t))
            except Exception:
                self.trace_ok = False

    def _learning_summary(self) -> dict | None:
        if self.learner is None:
            return None
        return {"mode": self.mode, "policy_revision": self.learner.table.revision,
                "table_states": len(self.learner.table), **self.learner.schedule.to_json()}

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log_observation(self, obs: Observation) -> None:
        self.departed.update(obs.departed_ids)
        self.arrived.update(obs.arrived_ids)
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
            if log_submission:  # the runtime only submits when timing allows, so any block is unexpected
                self.writer.append("error", t, ev.unexpected_block_payload(result))
                self.counters["unexpected_blocks"] += 1
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
        if self.writer.failed is not None:  # never write to a known-failed log
            self.trace_ok = False
            self._say(f"run stopped: {type(exc).__name__}: {exc}")
            return
        payload = {"stage": stage, "recovered": False, "kind": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, SensorFailure):
            payload["failures"] = ev.to_jsonable(exc.failures)
        try:
            self.writer.append("error", self.adapter.time if self.adapter.time is not None else 0, payload)
        except Exception:
            self.trace_ok = False  # the error itself could not be recorded; reported in RunResult


# ----------------------------------------------------------------------
# Training: session (checkpoint bookkeeping) and episodes
# ----------------------------------------------------------------------

class TrainingSession:
    """Learner plus progress plus checkpoint store for one training folder.

    Counts completed updates since the last successful save; a save is due at the configured
    interval. After a failed save the destination is never retried in this process.
    """

    def __init__(self, config: Config, training_id: str, learner: Learner, progress, compat: dict, store):
        self.config = config
        self.training_id = training_id
        self.learner = learner
        self.progress = progress
        self.compat = compat
        self.store = store
        self.updates_at_last_save = learner.completed_updates
        self.failed: str | None = None
        self.last_checkpoint_id: str | None = None

    def due(self) -> bool:
        return (self.learner.completed_updates - self.updates_at_last_save
                >= self.config.storage.checkpoint_every_updates)

    def begin_attempt(self) -> int:
        """Allocate the next persistent episode-attempt index (0-based)."""
        index = self.progress.episode_attempts
        self.progress = replace(self.progress, episode_attempts=index + 1)
        return index

    def episode_completed(self) -> None:
        self.progress = replace(self.progress, completed_episodes=self.progress.completed_episodes + 1)

    def progress_summary(self) -> dict:
        lr = self.learner
        return {"completed_episodes": self.progress.completed_episodes,
                "episode_attempts": self.progress.episode_attempts,
                "completed_updates": lr.completed_updates, "policy_revision": lr.table.revision,
                "accepted_choices": lr.schedule.accepted_choices, "epsilon": lr.schedule.epsilon}

    def save(self, reason: str, run_id: str | None, t: int | None):
        from traffic_cps.checkpoints import CheckpointWriteError, build_checkpoint

        if self.failed is not None:
            raise CheckpointWriteError(f"checkpoint destination already failed ({self.failed}); not retrying")
        generation = self.progress.save_generation + 1
        data = build_checkpoint(self.learner, replace(self.progress, save_generation=generation),
                                self.training_id, self.compat,
                                {"run_id": run_id, "reason": reason, "sim_time_s": t})
        try:
            result = self.store.save(data)
        except CheckpointWriteError as exc:
            self.failed = str(exc)
            raise
        self.progress = replace(self.progress, save_generation=generation)
        self.updates_at_last_save = self.learner.completed_updates
        self.last_checkpoint_id = result.checkpoint_id
        return result


@dataclass
class TrainingResult:
    training_dir: Path
    learner: Learner
    episodes: list[RunResult] = field(default_factory=list)
    development: list[dict] = field(default_factory=list)   # one record per milestone x development seed
    target_completed: int = 0
    completed_episodes: int = 0
    stopped: str | None = None                               # interrupt reason, if any
    persistence_failure: str | None = None

    @property
    def ok(self) -> bool:
        return (self.completed_episodes == self.target_completed and self.stopped is None
                and self.persistence_failure is None and all(d["ok"] for d in self.development)
                and all(r.persistence_ok and r.checkpoint_status == "saved" for r in self.episodes))


TRAINING_MANIFEST = "training.manifest.json"


def _training_dir_of(checkpoint: Path) -> Path:
    parent = checkpoint.resolve().parent
    if parent.name not in ("checkpoints", "milestones"):
        raise CheckpointError(f"{checkpoint} is not inside a training folder's checkpoints/ or milestones/")
    return parent.parent


def _allocated_attempts(training_dir: Path) -> int:
    """1 + the highest attempt index recorded in any run manifest under runs/ (0 if none).

    Every run folder must have a readable manifest with an integer attempt index; otherwise resume
    stops with a recovery error instead of guessing (and possibly reusing a seed)."""
    runs = training_dir / "runs"
    if not runs.is_dir():
        return 0
    highest = -1
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        path = run_dir / ev.RUN_MANIFEST
        try:
            index = json.loads(path.read_text(encoding="utf-8"))["provenance"]["episode_attempt_index"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CheckpointError(f"recovery error: {path} is missing or unreadable ({exc}); cannot tell which "
                                  f"traffic seeds were already used, so resume stops") from exc
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise CheckpointError(f"recovery error: {path} has invalid episode_attempt_index {index!r}")
        highest = max(highest, index)
    return highest + 1


def train(config: Config, episodes: int | None = None, output_dir: Path | str | None = None,
          horizon_s: int | None = None, notify: Callable[[str], None] | None = None, *,
          resume: Path | str | None = None, additional_episodes: int | None = None,
          stop: StopRequest | None = None, on_episode: Callable[[RunResult], None] | None = None,
          connection_wrapper: Callable[[Any], Any] | None = None) -> TrainingResult:
    """Fresh training (`episodes`, `output_dir`) or resumed training (`resume`, `additional_episodes`).

    Fresh training writes an initial checkpoint before any traffic. Resume validates the explicitly
    selected checkpoint (format, integrity, compatibility including horizon) before any traffic and
    continues in the same training folder. Each attempt gets the next persistent attempt index, which
    selects its traffic seed and run ID. Training stops at the first incomplete attempt or failed save.
    After each milestone episode, an immutable copy is evaluated frozen on the development seeds.
    """
    from traffic_cps.checkpoints import (
        CheckpointStore,
        CheckpointWriteError,
        Progress,
        compatibility,
        file_sha256,
        learner_from_checkpoint,
        progress_from_checkpoint,
    )
    from traffic_cps.controllers.q_learning import QLearningController
    from traffic_cps.scenario import create_scenario, profile_for_index, read_network_manifest

    horizon = config.simulation.horizon_s if horizon_s is None else horizon_s
    compat = compatibility(config, read_network_manifest(config.network.directory), horizon)
    say = notify or (lambda message: None)

    if resume is None:
        if isinstance(episodes, bool) or not isinstance(episodes, int) or episodes <= 0:
            raise ValueError(f"episodes must be a positive integer, got {episodes!r}")
        training_dir = Path(output_dir)
        training_dir.mkdir(parents=True, exist_ok=False)  # explicit fresh training: a new folder
        store = CheckpointStore(training_dir / "checkpoints")
        learner = Learner(config)
        session = TrainingSession(config, training_dir.name, learner, Progress(0, 0, 0), compat, store)
        ev.write_json_exclusive(training_dir / TRAINING_MANIFEST, {
            "training_id": training_dir.name, "created_at_utc": _utc_now(), "horizon_s": horizon,
            "config_sha256": config_digest(config), "compatibility": compat,
            "controller_seed": config.learning.controller_seed,
            "rng": "python random.Random (MT19937)",
        })
        result = TrainingResult(training_dir, learner, target_completed=episodes)
        try:
            session.save("initial", None, None)          # before any traffic
        except CheckpointWriteError as exc:
            result.persistence_failure = f"initial checkpoint failed: {exc}"
            return result
        source = {"kind": "fresh", "initial_checkpoint": session.last_checkpoint_id}
    else:
        if isinstance(additional_episodes, bool) or not isinstance(additional_episodes, int) \
                or additional_episodes <= 0:
            raise ValueError(f"additional episodes must be a positive integer, got {additional_episodes!r}")
        resume = Path(resume)
        training_dir = _training_dir_of(resume)
        store = CheckpointStore(training_dir / "checkpoints")
        data = store.load(resume, compat, "resume")
        if data["training_id"] != training_dir.name:
            raise CheckpointError(f"checkpoint belongs to training {data['training_id']!r}, "
                                  f"not folder {training_dir.name!r}")
        learner = learner_from_checkpoint(config, data)
        progress = progress_from_checkpoint(data)
        # Section B: the next attempt index exceeds both the checkpoint allocation and every attempt
        # allocated in a run manifest (failed runs included), so an explicitly selected older
        # checkpoint rolls back learning but never reuses a run identity or traffic seed.
        allocated = _allocated_attempts(training_dir)
        if allocated > progress.episode_attempts:
            say(f"run manifests in {training_dir.name} record {allocated} allocated attempts; the checkpoint "
                f"records {progress.episode_attempts}; continuing from attempt {allocated + 1}")
            progress = replace(progress, episode_attempts=allocated)
        session = TrainingSession(config, training_dir.name, learner, progress, compat, store)
        session.last_checkpoint_id = data["checkpoint_id"]
        result = TrainingResult(training_dir, learner, target_completed=progress.completed_episodes
                                + additional_episodes, completed_episodes=progress.completed_episodes)
        source = {"kind": "resumed", "checkpoint_path": str(resume), "checkpoint_id": data["checkpoint_id"],
                  "checkpoint_file_sha256": file_sha256(resume), "integrity_sha256": data["integrity_sha256"]}

    held_out = set(config.experiment.development_seeds) | set(config.experiment.final_seeds)
    while session.progress.completed_episodes < result.target_completed:
        if stop is not None and stop.requested:
            result.stopped = stop.reason
            break
        attempt = session.begin_attempt()
        seed = config.experiment.training_seed_start + attempt
        if seed in held_out:
            raise ValueError(f"training seed {seed} is a development or final seed")
        profile = profile_for_index(attempt)
        scenario = training_dir / "scenarios" / f"seed_{seed:03d}"
        create_scenario(config.network.directory, scenario, seed, profile, horizon)
        runtime = Runtime(config, scenario, QLearningController(learner), training_dir, horizon_s=horizon,
                          mode=Mode.TRAIN, notify=notify, learner=learner, session=session, stop=stop,
                          connection_wrapper=connection_wrapper,
                          run_id=f"train-attempt{attempt + 1:03d}-seed{seed:03d}",
                          provenance={"training_id": training_dir.name, "episode_attempt_index": attempt,
                                      "traffic_seed": seed, "profile": profile.value, "learning_source": source})
        run = runtime.run()
        result.episodes.append(run)
        result.completed_episodes = session.progress.completed_episodes
        if on_episode is not None:
            on_episode(run)
        source = {"kind": "continued_in_memory", "previous_run": run.run_id,
                  "last_checkpoint": session.last_checkpoint_id}
        if run.completion is not CompletionStatus.COMPLETED or run.checkpoint_status != "saved" \
                or not run.persistence_ok:
            if stop is not None and stop.requested:
                result.stopped = stop.reason
            if run.checkpoint_status != "saved":
                result.persistence_failure = run.failure
            break
        n = session.progress.completed_episodes
        if n in config.experiment.development_after_episodes:
            try:
                milestone = store.write_milestone(training_dir / "milestones", n)
            except CheckpointWriteError as exc:
                result.persistence_failure = str(exc)
                say(f"milestone write failed: {exc}")
                break
            result.development += evaluate_milestone(config, milestone, training_dir, horizon, n,
                                                     notify=notify, stop=stop)
    return result


def evaluate_milestone(config: Config, milestone: Path, training_dir: Path, horizon_s: int, episode: int,
                       notify: Callable[[str], None] | None = None, stop: StopRequest | None = None) -> list[dict]:
    """Frozen evaluation of an immutable milestone copy on each development seed.

    Each seed loads its own Learner from the file (own table copy and RNG), so evaluation cannot
    touch training state. Failures are recorded and reported, never raised: training continues.
    """
    from traffic_cps.checkpoints import CheckpointStore, compatibility, file_sha256, learner_from_checkpoint
    from traffic_cps.controllers.q_learning import QLearningController
    from traffic_cps.scenario import create_scenario, profile_for_index

    records = []
    for index, seed in enumerate(config.experiment.development_seeds):
        record = {"episode": episode, "seed": seed, "ok": False, "run_id": None, "run_dir": None, "failure": None}
        try:
            scenario = training_dir / "scenarios" / f"dev_seed_{seed:03d}"
            if not (scenario / SCENARIO_MANIFEST).is_file():
                create_scenario(config.network.directory, scenario, seed, profile_for_index(index), horizon_s)
            with open(scenario / SCENARIO_MANIFEST, encoding="utf-8") as fh:
                network = json.load(fh)["network"]
            data = CheckpointStore(milestone.parent).load(milestone, compatibility(config, network, horizon_s),
                                                          "evaluate")
            learner = learner_from_checkpoint(config, data)
            run = Runtime(config, scenario, QLearningController(learner),
                          training_dir / "development" / f"episode_{episode:03d}", horizon_s=horizon_s,
                          mode=Mode.EVALUATE, notify=notify, learner=learner, stop=stop,
                          run_id=f"dev-ep{episode:03d}-seed{seed:03d}",
                          provenance={"purpose": "development_evaluation", "traffic_seed": seed,
                                      "checkpoint_path": str(milestone), "checkpoint_id": data["checkpoint_id"],
                                      "checkpoint_file_sha256": file_sha256(milestone)}).run()
            record["run_id"] = run.run_id
            record["run_dir"] = run.run_dir
            record["ok"] = run.completion is CompletionStatus.COMPLETED and run.persistence_ok
            record["failure"] = run.failure
        except Exception as exc:  # reported separately; training state is untouched either way
            record["failure"] = f"{type(exc).__name__}: {exc}"
        if not record["ok"] and notify is not None:
            notify(f"development evaluation episode {episode} seed {seed} failed: {record['failure']}")
        records.append(record)
    return records
