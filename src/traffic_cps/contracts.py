"""Typed immutable records and enums shared across modules (build contract, Section D).

Records are added by the chunk that first produces them, so no field is
guessed ahead of the module that defines it. Mapping fields are wrapped in
read-only proxies so a frozen record cannot be mutated through its dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Direction(str, Enum):
    NS = "NS"
    WE = "WE"

    @property
    def other(self) -> "Direction":
        return Direction.WE if self is Direction.NS else Direction.NS


class Mode(str, Enum):
    TRAIN = "train"
    EVALUATE = "evaluate"


class SignalPhase(str, Enum):
    NS_GREEN = "NS_GREEN"
    NS_YELLOW = "NS_YELLOW"
    WE_GREEN = "WE_GREEN"
    WE_YELLOW = "WE_YELLOW"

    @staticmethod
    def green(direction: Direction) -> "SignalPhase":
        return SignalPhase(f"{direction.value}_GREEN")

    @staticmethod
    def yellow(direction: Direction) -> "SignalPhase":
        return SignalPhase(f"{direction.value}_YELLOW")


class SignalColor(str, Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"


class OriginKind(str, Enum):
    """Who caused a signal write. Kept distinct so explanations never conflate sources."""

    INITIALIZATION = "initialization"
    CONTROLLER_DECISION = "controller_decision"
    OPERATING_ACTION = "operating_action"
    TRANSITION_COMPLETION = "transition_completion"
    TEST = "test"


@dataclass(frozen=True)
class Origin:
    kind: OriginKind
    origin_id: str


class SubmissionStatus(str, Enum):
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


class BlockReason(str, Enum):
    MINIMUM_GREEN_NOT_REACHED = "minimum_green_not_reached"
    TRANSITION_IN_PROGRESS = "transition_in_progress"
    AT_OR_AFTER_HORIZON = "at_or_after_horizon"
    SAFETY_VETO = "safety_veto"


def _frozen_mapping(obj, *names: str) -> None:
    for name in names:
        object.__setattr__(obj, name, MappingProxyType(dict(getattr(obj, name))))


@dataclass(frozen=True)
class RawSnapshot:
    """One raw retrieval from the simulator at one simulated second (no time advance)."""

    time: int
    signal_state: str
    halting_by_lane: Mapping[str, int]
    departed_ids: tuple[str, ...]
    arrived_ids: tuple[str, ...]
    teleport_ids: tuple[str, ...]
    colliding_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _frozen_mapping(self, "halting_by_lane")


@dataclass(frozen=True)
class SignalTiming:
    """The adapter's authoritative timing state, attached to an observation."""

    time: int
    mask: str
    phase: SignalPhase
    current_green: Direction | None
    green_onset_s: int | None
    transition_target: Direction | None
    transition_start_s: int | None
    colors: Mapping[Direction, SignalColor]
    color_since_s: Mapping[Direction, int]

    def __post_init__(self) -> None:
        _frozen_mapping(self, "colors", "color_since_s")
        if (self.current_green is None) == (self.transition_target is None):
            raise ValueError("exactly one of current_green and transition_target must be set")

    @property
    def elapsed_green_s(self) -> int | None:
        return None if self.green_onset_s is None else self.time - self.green_onset_s


@dataclass(frozen=True)
class RetrievalFailure:
    """One failed retrieval attempt, kept for the failure log (never replaced by a default)."""

    attempt: int
    time: int
    field: str
    error: str


@dataclass(frozen=True)
class MeasurementQuality:
    attempts: int
    failures: tuple[RetrievalFailure, ...]
    source: str = "simulator_ground_truth"
    fault_label: str | None = None


@dataclass(frozen=True)
class Observation:
    observation_id: str
    time: int
    counts_by_lane: Mapping[str, int]
    counts_by_direction: Mapping[Direction, int]
    lane_scores: Mapping[str, float]
    scores: Mapping[Direction, float]
    colors: Mapping[Direction, SignalColor]
    color_since_s: Mapping[Direction, int]
    phase: SignalPhase
    current_green: Direction | None
    elapsed_green_s: int | None
    transition_target: Direction | None
    transition_start_s: int | None
    raw_signal: str
    measurement: MeasurementQuality

    def __post_init__(self) -> None:
        _frozen_mapping(self, "counts_by_lane", "counts_by_direction", "lane_scores", "scores",
                        "colors", "color_since_s")


@dataclass(frozen=True)
class SafetyVerdict:
    allowed: bool
    invariant: str | None
    reason: str


@dataclass(frozen=True)
class SafetyOverride:
    """A proposed write the monitor blocked (pre-command check)."""

    time: int
    proposed_mask: str
    current_mask: str | None
    origin: Origin
    invariant: str
    reason: str


@dataclass(frozen=True)
class SafetyViolation:
    """An observed state that broke an invariant (post-observation check). Stops the run."""

    time: int
    invariant: str
    raw_signal: str
    previous_raw_signal: str | None
    stage: str
    detail: str


@dataclass(frozen=True)
class SignalEvent:
    """An observed, read-back-confirmed signal change."""

    event_id: str
    time: int
    old_mask: str
    new_mask: str
    old_colors: Mapping[Direction, SignalColor]
    new_colors: Mapping[Direction, SignalColor]
    origin: Origin
    transition_target: Direction | None
    monitor_result: SafetyVerdict

    def __post_init__(self) -> None:
        _frozen_mapping(self, "old_colors", "new_colors")


@dataclass(frozen=True)
class SubmissionResult:
    """Outcome of one write request. ACCEPTED means submitted, not that the target is green yet."""

    origin: Origin
    time: int
    target: Direction
    status: SubmissionStatus
    block_reason: BlockReason | None = None
    error: str | None = None
    signal_event: SignalEvent | None = None
    override: SafetyOverride | None = None
    violations: tuple[SafetyViolation, ...] = ()
