"""Traffic sensor: validated counts and queue scores (Module 1).

The sensor reports traffic; it never chooses a signal. Readings come from
simulator ground truth (halting vehicles, speed < 0.1 m/s), not a modeled
loop detector. A missing or invalid reading is retried at the same simulated
second and then reported as failure; it is never replaced by zero or by an
earlier value.
"""

from __future__ import annotations

from typing import Callable, Mapping

from traffic_cps.contracts import (
    Direction,
    MeasurementQuality,
    Observation,
    RawSnapshot,
    RetrievalFailure,
    SignalTiming,
)


class RetrievalError(RuntimeError):
    """One failed raw retrieval, naming the field that could not be read."""

    def __init__(self, field: str, error: str):
        super().__init__(f"{field}: {error}")
        self.field = field
        self.error = error


class SensorFailure(RuntimeError):
    """Retrieval failed on every allowed attempt, or an observation is inconsistent."""

    def __init__(self, message: str, failures: tuple[RetrievalFailure, ...] = ()):
        super().__init__(message)
        self.failures = failures


class TrafficSensor:
    def __init__(
        self,
        lanes: Mapping[Direction, tuple[str, ...]],
        lane_lengths_m: Mapping[str, float],
        vehicle_length_m: float,
        minimum_gap_m: float,
        retries: int,
    ):
        self.lanes = {Direction(d): tuple(ids) for d, ids in lanes.items()}
        self.lane_order = tuple(lane for d in Direction for lane in self.lanes[d])
        self.retries = retries
        spacing = vehicle_length_m + minimum_gap_m
        self.capacity: dict[str, float] = {}
        for lane in self.lane_order:
            length = lane_lengths_m.get(lane)
            if not isinstance(length, (int, float)) or not length > 0:
                raise ValueError(f"lane {lane}: length must be positive, got {length!r}")
            # Project-defined estimate of how many stopped cars fit, not a guaranteed maximum.
            self.capacity[lane] = float(length) / spacing

    # ------------------------------------------------------------------
    # Retrieval with the same-second retry policy
    # ------------------------------------------------------------------

    def read(
        self,
        read_raw: Callable[[], RawSnapshot],
        expected_time: int,
        notify: Callable[[str], None] | None = None,
    ) -> tuple[RawSnapshot, MeasurementQuality]:
        """Up to 1 + retries attempts at one simulated second; no time advance in between."""
        failures: list[RetrievalFailure] = []
        attempts = 1 + self.retries
        for attempt in range(1, attempts + 1):
            try:
                raw = read_raw()
                self._validate(raw, expected_time)
            except RetrievalError as exc:
                failures.append(RetrievalFailure(attempt, expected_time, exc.field, exc.error))
            except Exception as exc:  # any other read exception is also a failed attempt
                failures.append(RetrievalFailure(attempt, expected_time, "unknown", f"{type(exc).__name__}: {exc}"))
            else:
                return raw, MeasurementQuality(attempts=attempt, failures=tuple(failures))
            if attempt < attempts and notify is not None:
                last = failures[-1]
                notify(f"t={expected_time}: retrieval of {last.field} failed ({last.error}); "
                       f"retrying ({attempt}/{self.retries})")
        last = failures[-1]
        raise SensorFailure(
            f"t={expected_time}: retrieval failed on all {attempts} attempts "
            f"(last: {last.field}: {last.error})",
            tuple(failures),
        )

    def _validate(self, raw: RawSnapshot, expected_time: int) -> None:
        if raw.time != expected_time:
            raise RetrievalError("time", f"snapshot time {raw.time!r} != expected {expected_time}")
        for lane in self.lane_order:
            if lane not in raw.halting_by_lane:
                raise RetrievalError(lane, "missing halting count")
            count = raw.halting_by_lane[lane]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise RetrievalError(lane, f"invalid halting count {count!r}")

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(
        self,
        raw: RawSnapshot,
        timing: SignalTiming,
        quality: MeasurementQuality,
        observation_id: str | None = None,
    ) -> Observation:
        """Build one observation; counts, scores, and colors share one timestamp."""
        try:
            self._validate(raw, raw.time)
        except RetrievalError as exc:
            raise SensorFailure(f"invalid snapshot: {exc}") from exc
        if timing.time != raw.time:
            raise SensorFailure(f"signal timing at t={timing.time} does not match snapshot t={raw.time}")

        counts = {lane: raw.halting_by_lane[lane] for lane in self.lane_order}
        lane_scores = {lane: min(1.0, counts[lane] / self.capacity[lane]) for lane in self.lane_order}
        return Observation(
            observation_id=observation_id or f"obs-{raw.time}",
            time=raw.time,
            counts_by_lane=counts,
            counts_by_direction={d: sum(counts[lane] for lane in self.lanes[d]) for d in Direction},
            lane_scores=lane_scores,
            scores={d: sum(lane_scores[lane] for lane in self.lanes[d]) for d in Direction},
            colors=dict(timing.colors),
            color_since_s=dict(timing.color_since_s),
            phase=timing.phase,
            current_green=timing.current_green,
            elapsed_green_s=timing.elapsed_green_s,
            transition_target=timing.transition_target,
            transition_start_s=timing.transition_start_s,
            raw_signal=timing.mask,
            measurement=quality,
        )
