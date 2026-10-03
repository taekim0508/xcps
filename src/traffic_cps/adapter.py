"""Simulation adapter: the single owner of the TraCI connection and signal timing (Module 4).

Only this class writes signal state. Every low-level write is checked by the
safety monitor first and read back immediately; only the read-back confirms a
change. Time advances only when the runtime calls `advance_one_second`. The
adapter never selects a direction: it executes or blocks requests and reports
exactly what happened.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import traci

from traffic_cps.config import Config, sha256_file
from traffic_cps.contracts import (
    BlockReason,
    Direction,
    Observation,
    Origin,
    OriginKind,
    RawSnapshot,
    SafetyVerdict,
    SignalColor,
    SignalEvent,
    SignalPhase,
    SignalTiming,
    SubmissionResult,
    SubmissionStatus,
)
from traffic_cps.safety import SafetyMonitor
from traffic_cps.scenario import NETWORK_FILE, ROUTES_FILE, SCENARIO_MANIFEST
from traffic_cps.sensor import RetrievalError

LANE_LENGTH_TOLERANCE_M = 1e-6
_COLOR_CHARS = {SignalColor.GREEN: "G", SignalColor.YELLOW: "y", SignalColor.RED: "r"}
_CHAR_COLORS = {char: color for color, char in _COLOR_CHARS.items()}


class AdapterError(RuntimeError):
    """Initialization, integrity, or command failure. The run must stop."""


# ----------------------------------------------------------------------
# Signal-index mapping
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class SignalMap:
    """Which signal-string index belongs to which direction, derived from SUMO's links."""

    index_directions: tuple[Direction, ...]
    movements: tuple[tuple[str, str], ...]

    @classmethod
    def from_controlled_links(cls, links: Any, lanes: Mapping[Direction, tuple[str, ...]]) -> "SignalMap":
        lane_direction = {lane: Direction(d) for d, ids in lanes.items() for lane in ids}
        index_directions: list[Direction] = []
        movements: list[tuple[str, str]] = []
        for index, group in enumerate(links):
            if not group:
                raise AdapterError(f"signal index {index} controls no movement")
            directions = set()
            for link in group:
                incoming, outgoing = link[0], link[1]
                if incoming not in lane_direction:
                    raise AdapterError(f"signal index {index} controls unconfigured lane {incoming}")
                directions.add(lane_direction[incoming])
                movements.append((incoming, outgoing))
            if len(directions) != 1:
                raise AdapterError(f"signal index {index} mixes directions {sorted(d.value for d in directions)}")
            index_directions.append(directions.pop())
        controlled = {incoming for incoming, _ in movements}
        missing = sorted(set(lane_direction) - controlled)
        if missing:
            raise AdapterError(f"configured lanes not controlled by the signal: {missing}")
        return cls(tuple(index_directions), tuple(movements))

    def mask(self, phase: SignalPhase) -> str:
        active = Direction(phase.value.split("_")[0])
        color = SignalColor.GREEN if phase.value.endswith("GREEN") else SignalColor.YELLOW
        return "".join(_COLOR_CHARS[color] if d is active else "r" for d in self.index_directions)

    def colors(self, mask: str) -> dict[Direction, SignalColor]:
        if len(mask) != len(self.index_directions):
            raise AdapterError(f"signal mask {mask!r} has wrong length")
        seen: dict[Direction, set[SignalColor]] = {d: set() for d in Direction}
        for char, direction in zip(mask, self.index_directions):
            if char not in _CHAR_COLORS:
                raise AdapterError(f"signal mask {mask!r} has unexpected state {char!r}")
            seen[direction].add(_CHAR_COLORS[char])
        if any(len(c) != 1 for c in seen.values()):
            raise AdapterError(f"signal mask {mask!r} shows two colors for one direction")
        return {d: c.pop() for d, c in seen.items()}


# ----------------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class _WriteOutcome:
    status: SubmissionStatus
    verdict: SafetyVerdict
    event: SignalEvent | None = None
    error: str | None = None
    violations: tuple = ()
    override: Any = None


class SumoAdapter:
    def __init__(
        self,
        config: Config,
        monitor: SafetyMonitor,
        connection_wrapper: Callable[[Any], Any] | None = None,
        sumo_log: Path | None = None,
    ):
        self.config = config
        self.monitor = monitor
        self._wrap = connection_wrapper
        self._sumo_log = sumo_log
        self._conn: Any = None
        self._label: str | None = None
        self._enforce_timing = True  # only faults.disable_adapter_timing_guard turns this off
        self._event_seq = 0

        self.tls_id = config.simulation.tls_id
        self.yellow_s = config.signal.yellow_s
        self.minimum_green_s = config.signal.minimum_green_s
        self.green_service_limit_s = config.signal.green_service_limit_s

        self.horizon_s: int | None = None
        self.time: int | None = None
        self.signal_map: SignalMap | None = None
        self.lane_lengths: dict[str, float] = {}
        self.failed: str | None = None
        self.last_verified_mask: str | None = None
        self.unconfirmed_mask: str | None = None

        self._phase: SignalPhase | None = None
        self._current_green: Direction | None = None
        self._green_onset: int | None = None
        self._target: Direction | None = None
        self._transition_start: int | None = None
        self._transition_origin: Origin | None = None
        self._color_since: dict[Direction, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, scenario_dir: Path | str, horizon_s: int | None = None) -> RawSnapshot:
        """Launch SUMO, validate links and lanes, write the initial green, return the t=0 snapshot."""
        if self._conn is not None:
            raise AdapterError("adapter already started")
        scenario_dir = Path(scenario_dir)
        manifest = self._load_manifest(scenario_dir)
        horizon = self.config.simulation.horizon_s if horizon_s is None else horizon_s
        if not isinstance(horizon, int) or horizon <= 0 or horizon % 5 != 0:
            raise AdapterError(f"run horizon must be a positive integer divisible by 5, got {horizon!r}")
        if manifest["demand"]["horizon_s"] != horizon:
            raise AdapterError(f"scenario horizon {manifest['demand']['horizon_s']}s does not match "
                               f"run horizon {horizon}s")
        self.horizon_s = horizon

        binary = "sumo-gui" if self.config.simulation.gui else "sumo"
        cmd = [
            binary,
            "-n", str(scenario_dir / NETWORK_FILE),
            "-r", str(scenario_dir / ROUTES_FILE),
            "--step-length", "1",
            "--begin", "0",
            "--end", str(horizon + 1),
            "--seed", str(manifest["demand"]["traffic_seed"]),
            "--time-to-teleport", "-1",
            "--collision.action", "warn",
            "--collision.check-junctions", "true",
            "--no-step-log", "true",
        ]
        if self._sumo_log is not None:
            cmd += ["--log", str(self._sumo_log)]
        self._label = f"traffic-cps-{uuid.uuid4().hex}"
        try:
            traci.start(cmd, label=self._label, stdout=subprocess.DEVNULL)
            conn = traci.getConnection(self._label)
        except Exception as exc:
            raise AdapterError(f"cannot start SUMO: {exc}") from exc
        self._conn = self._wrap(conn) if self._wrap else conn
        try:
            return self._initialize(manifest)
        except Exception:
            self.close()
            raise

    def _load_manifest(self, scenario_dir: Path) -> dict:
        path = scenario_dir / SCENARIO_MANIFEST
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdapterError(f"cannot read scenario manifest {path}: {exc}") from exc
        for name in (NETWORK_FILE, ROUTES_FILE):
            file = scenario_dir / name
            if not file.is_file() or sha256_file(file) != manifest["sha256"].get(name):
                raise AdapterError(f"{file} is missing or does not match the scenario manifest")
        return manifest

    def _initialize(self, manifest: dict) -> RawSnapshot:
        conn = self._conn
        self.time = self._read_time_strict()
        if self.time != 0:
            raise AdapterError(f"simulation starts at t={self.time}, expected 0")

        lanes = self.config.network.lanes_by_direction()
        self.signal_map = SignalMap.from_controlled_links(conn.trafficlight.getControlledLinks(self.tls_id), lanes)
        expected = sorted(tuple(m) for m in manifest["network"]["movements"])
        if sorted(self.signal_map.movements) != expected:
            raise AdapterError(f"live controlled movements {sorted(self.signal_map.movements)} "
                               f"do not match the manifest {expected}")

        manifest_lengths = manifest["network"]["incoming_lane_lengths_m"]
        for lane in (lane for ids in lanes.values() for lane in ids):
            length = float(conn.lane.getLength(lane))
            if not length > 0:
                raise AdapterError(f"lane {lane} has nonpositive length {length}")
            if lane not in manifest_lengths or abs(manifest_lengths[lane] - length) > LANE_LENGTH_TOLERANCE_M:
                raise AdapterError(f"lane {lane} length {length} does not match manifest "
                                   f"{manifest_lengths.get(lane)}")
            self.lane_lengths[lane] = length

        self.monitor.bind(self.signal_map.index_directions, self.horizon_s)
        initial = self.config.simulation.initial_green
        origin = Origin(OriginKind.INITIALIZATION, "init")
        outcome = self._write(self.signal_map.mask(SignalPhase.green(initial)), origin, None)
        if outcome.status is not SubmissionStatus.ACCEPTED:
            raise AdapterError(f"initial signal write failed: {outcome.error or outcome.verdict.reason}")

        self._phase = SignalPhase.green(initial)
        self._current_green = initial
        self._green_onset = 0
        self._color_since = {d: 0 for d in Direction}
        self.initial_event = outcome.event
        return self.read_raw()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass  # closing is best effort; the run status already records any failure
            self._conn = None

    def __enter__(self) -> "SumoAdapter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Time and reading
    # ------------------------------------------------------------------

    def _read_time_strict(self) -> int:
        value = self._conn.simulation.getTime()
        if not isinstance(value, (int, float)) or float(value) != int(value):
            raise AdapterError(f"simulation time {value!r} is not an integer second")
        return int(value)

    def advance_one_second(self) -> int:
        """Advance exactly one simulated second (called only by the runtime)."""
        if self._conn is None:
            raise AdapterError("adapter is not started")
        if self.failed:
            raise AdapterError(f"adapter stopped: {self.failed}")
        if self.time >= self.horizon_s:
            raise AdapterError(f"cannot advance at or beyond the horizon ({self.horizon_s}s)")
        try:
            self._conn.simulationStep()
            now = self._read_time_strict()
        except AdapterError:
            raise
        except Exception as exc:
            self.failed = f"simulation step failed: {exc}"
            raise AdapterError(self.failed) from exc
        if now != self.time + 1:
            self.failed = f"simulation time moved from {self.time} to {now}, expected {self.time + 1}"
            raise AdapterError(self.failed)
        self.time = now
        return now

    def read_raw(self) -> RawSnapshot:
        """One raw retrieval at the current second. Never advances time or writes."""
        conn = self._conn

        def get(field: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as exc:
                raise RetrievalError(field, f"{type(exc).__name__}: {exc}") from exc

        raw_time = get("time", conn.simulation.getTime)
        if not isinstance(raw_time, (int, float)) or float(raw_time) != int(raw_time):
            raise RetrievalError("time", f"not an integer second: {raw_time!r}")
        signal = get("signal_state", lambda: conn.trafficlight.getRedYellowGreenState(self.tls_id))
        if not isinstance(signal, str):
            raise RetrievalError("signal_state", f"expected a string, got {signal!r}")
        halting = {}
        for ids in self.config.network.lanes_by_direction().values():
            for lane in ids:
                halting[lane] = get(lane, lambda lane=lane: conn.lane.getLastStepHaltingNumber(lane))
        return RawSnapshot(
            time=int(raw_time),
            signal_state=signal,
            halting_by_lane=halting,
            departed_ids=tuple(get("departed_ids", conn.simulation.getDepartedIDList)),
            arrived_ids=tuple(get("arrived_ids", conn.simulation.getArrivedIDList)),
            teleport_ids=tuple(get("teleport_ids", conn.simulation.getStartingTeleportIDList)),
            colliding_ids=tuple(get("colliding_ids", conn.simulation.getCollidingVehiclesIDList)),
        )

    def confirm(self, raw: RawSnapshot) -> None:
        """Accept a tick snapshot only if the plant shows exactly what was last verified."""
        problem = None
        if raw.time != self.time:
            problem = f"snapshot time {raw.time} != adapter time {self.time}"
        elif raw.colliding_ids:
            problem = f"collision involving {list(raw.colliding_ids)} at t={raw.time}"
        elif raw.teleport_ids:
            problem = f"unexpected teleport of {list(raw.teleport_ids)} at t={raw.time}"
        elif raw.signal_state != self.last_verified_mask:
            problem = (f"unexpected signal change at t={raw.time}: plant shows {raw.signal_state!r}, "
                       f"last verified {self.last_verified_mask!r}")
        if problem:
            self.failed = problem
            raise AdapterError(problem)

    # ------------------------------------------------------------------
    # Timing state (authoritative)
    # ------------------------------------------------------------------

    def timing(self) -> SignalTiming:
        return SignalTiming(
            time=self.time,
            mask=self.last_verified_mask,
            phase=self._phase,
            current_green=self._current_green,
            green_onset_s=self._green_onset,
            transition_target=self._target,
            transition_start_s=self._transition_start,
            colors=self.signal_map.colors(self.last_verified_mask),
            color_since_s=dict(self._color_since),
        )

    def elapsed_green(self) -> int | None:
        return None if self._green_onset is None else self.time - self._green_onset

    def choice_allowed(self) -> bool:
        """Timing permits a free choice now (green, minimum green complete, before horizon)."""
        return (
            not self.failed
            and self.time < self.horizon_s
            and self._target is None
            and self.elapsed_green() >= self.minimum_green_s
        )

    def service_limit_reached(self) -> bool:
        return self._current_green is not None and self.elapsed_green() >= self.green_service_limit_s

    # ------------------------------------------------------------------
    # Requests and transitions
    # ------------------------------------------------------------------

    def request(self, target: Direction, origin: Origin, observation: Observation | None = None) -> SubmissionResult:
        """Retain, switch (start yellow), or block. ACCEPTED is submission, not green."""
        try:
            target = Direction(target)
        except ValueError as exc:
            self.failed = f"invalid request target {target!r}"
            raise AdapterError(self.failed) from exc
        if observation is not None and observation.time != self.time:
            raise AdapterError(f"request uses observation from t={observation.time} at t={self.time}")
        t = self.time

        def result(status: SubmissionStatus, **kw: Any) -> SubmissionResult:
            return SubmissionResult(origin=origin, time=t, target=target, status=status, **kw)

        if self.failed:
            return result(SubmissionStatus.FAILED, error=f"adapter stopped: {self.failed}")
        if t >= self.horizon_s:
            return result(SubmissionStatus.BLOCKED, block_reason=BlockReason.AT_OR_AFTER_HORIZON)
        if self._target is not None:
            return result(SubmissionStatus.BLOCKED, block_reason=BlockReason.TRANSITION_IN_PROGRESS)
        if target is self._current_green:
            return result(SubmissionStatus.ACCEPTED)  # retain: no write, timer untouched
        if self._enforce_timing and self.elapsed_green() < self.minimum_green_s:
            return result(SubmissionStatus.BLOCKED, block_reason=BlockReason.MINIMUM_GREEN_NOT_REACHED)

        outgoing = self._current_green
        outcome = self._write(self.signal_map.mask(SignalPhase.yellow(outgoing)), origin, target)
        if outcome.status is not SubmissionStatus.ACCEPTED:
            return self._failed_result(result, outcome)

        self._phase = SignalPhase.yellow(outgoing)
        self._current_green = None
        self._green_onset = None
        self._target = target
        self._transition_start = t
        self._transition_origin = origin
        self._color_since[outgoing] = t
        return result(SubmissionStatus.ACCEPTED, signal_event=outcome.event)

    def complete_due_transition(self) -> SubmissionResult | None:
        """Write the committed target green once yellow has lasted yellow_s (never at the horizon)."""
        if self._target is None or self.failed or self.time >= self.horizon_s:
            return None
        if self.time - self._transition_start < self.yellow_s:
            return None
        t, target = self.time, self._target
        origin = Origin(OriginKind.TRANSITION_COMPLETION, self._transition_origin.origin_id)

        def result(status: SubmissionStatus, **kw: Any) -> SubmissionResult:
            return SubmissionResult(origin=origin, time=t, target=target, status=status, **kw)

        outcome = self._write(self.signal_map.mask(SignalPhase.green(target)), origin, target)
        if outcome.status is not SubmissionStatus.ACCEPTED:
            return self._failed_result(result, outcome)

        self._color_since[target] = t
        self._color_since[target.other] = t
        self._phase = SignalPhase.green(target)
        self._current_green = target
        self._green_onset = t
        self._target = None
        self._transition_start = None
        self._transition_origin = None
        return result(SubmissionStatus.ACCEPTED, signal_event=outcome.event)

    @staticmethod
    def _failed_result(result: Callable[..., SubmissionResult], outcome: _WriteOutcome) -> SubmissionResult:
        if outcome.status is SubmissionStatus.BLOCKED:
            return result(SubmissionStatus.BLOCKED, block_reason=BlockReason.SAFETY_VETO, override=outcome.override)
        return result(outcome.status, error=outcome.error, violations=outcome.violations)

    def _write(self, mask: str, origin: Origin, target: Direction | None) -> _WriteOutcome:
        """Monitor check, write, immediate read-back. The only place signal state is written."""
        t = self.time
        verdict = self.monitor.check_proposed(mask, t, origin)
        if not verdict.allowed:
            return _WriteOutcome(SubmissionStatus.BLOCKED, verdict, override=self.monitor.overrides[-1])

        old_mask = self.last_verified_mask
        if old_mask is None:  # initialization: record whatever SUMO showed before our first write
            try:
                old_mask = self._conn.trafficlight.getRedYellowGreenState(self.tls_id)
            except Exception:
                old_mask = ""
        try:
            self._conn.trafficlight.setRedYellowGreenState(self.tls_id, mask)
        except Exception as exc:
            self.failed = f"signal write of {mask!r} failed at t={t}: {exc}"
            return _WriteOutcome(SubmissionStatus.FAILED, verdict, error=self.failed)
        try:
            readback = self._conn.trafficlight.getRedYellowGreenState(self.tls_id)
        except Exception as exc:
            self.failed = f"read-back after writing {mask!r} failed at t={t}: {exc}"
            self.unconfirmed_mask = mask
            return _WriteOutcome(SubmissionStatus.UNCONFIRMED, verdict, error=self.failed)

        violations = tuple(self.monitor.observe(readback, t, "write"))
        if readback != mask or violations:
            self.failed = (f"read-back {readback!r} after writing {mask!r} at t={t}"
                           + (f"; safety violations {[v.invariant for v in violations]}" if violations else ""))
            return _WriteOutcome(SubmissionStatus.FAILED, verdict, error=self.failed, violations=violations)

        self.last_verified_mask = readback
        self._event_seq += 1
        event = SignalEvent(
            event_id=f"sig-{self._event_seq}",
            time=t,
            old_mask=old_mask,
            new_mask=readback,
            old_colors=self._safe_colors(old_mask),
            new_colors=self.signal_map.colors(readback),
            origin=origin,
            transition_target=target,
            monitor_result=verdict,
        )
        return _WriteOutcome(SubmissionStatus.ACCEPTED, verdict, event=event)

    def _safe_colors(self, mask: str) -> dict[Direction, SignalColor]:
        try:
            return self.signal_map.colors(mask)
        except AdapterError:
            return {}  # SUMO's pre-initialization program state may use other characters
