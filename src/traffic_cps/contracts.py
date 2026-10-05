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
    # Truth versus measurement (Section H). counts_* and scores above are what the controller,
    # reward, and S4 use; true_* are the simulator's counts and feed congestion metrics. In clean
    # runs they are equal and fault_label is "clean".
    true_counts_by_lane: Mapping[str, int] | None = None
    true_counts_by_direction: Mapping[Direction, int] | None = None
    fault_label: str = "clean"
    departed_ids: tuple[str, ...] = ()   # vehicles inserted during this second (accounting)
    arrived_ids: tuple[str, ...] = ()    # vehicles that left the network during this second

    def __post_init__(self) -> None:
        _frozen_mapping(self, "counts_by_lane", "counts_by_direction", "lane_scores", "scores",
                        "colors", "color_since_s")
        if self.true_counts_by_lane is not None:
            _frozen_mapping(self, "true_counts_by_lane", "true_counts_by_direction")


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


# ----------------------------------------------------------------------
# Decisions
# ----------------------------------------------------------------------

class DecisionSource(str, Enum):
    """What produced a controller decision. Operating rules and safety are never decisions."""

    QUEUE_RULE = "queue_rule"
    FIXED_SCHEDULE = "fixed_schedule"
    Q_GREEDY = "q_greedy"              # highest learned value (current green wins ties)
    Q_EXPLORATION = "q_exploration"    # random training choice, even when it matches the greedy one
    QUEUE_FALLBACK = "queue_fallback"  # frozen evaluation without full coverage: the queue rule decides
    TEST_SCRIPT = "test_script"  # scripted decisions used only by tests


class DecisionReason(str, Enum):
    HIGHER_QUEUE_SCORE = "higher_queue_score"
    EQUAL_QUEUE_SCORES = "equal_queue_scores"
    SERVICE_LIMIT_COMPETING_DEMAND = "service_limit_competing_demand"
    FIXED_TARGET_REACHED = "fixed_target_reached"
    FIXED_TARGET_NOT_REACHED = "fixed_target_not_reached"
    HIGHER_Q_VALUE = "higher_q_value"
    EQUAL_Q_VALUES = "equal_q_values"
    EXPLORATION = "exploration"
    SCRIPTED = "scripted"


class FallbackReason(str, Enum):
    UNSEEN_STATE = "unseen_state"
    INCOMPLETE_ACTION_COVERAGE = "incomplete_action_coverage"


class HoldReason(str, Enum):
    """Why a scheduled control point produced no controller choice."""

    MINIMUM_GREEN_NOT_REACHED = "minimum_green_not_reached"
    TRANSITION_IN_PROGRESS = "transition_in_progress"


@dataclass(frozen=True)
class DecisionContext:
    observation: Observation
    allowed: tuple[Direction, ...]
    mode: Mode
    minimum_green_s: int
    green_service_limit_s: int
    tie_abs_tolerance: float
    score_cap: float


@dataclass(frozen=True)
class Decision:
    decision_id: str
    observation_id: str
    time: int
    requested: Direction
    current_green: Direction
    source: DecisionSource
    reason: DecisionReason
    scores: Mapping[Direction, float]
    counts: Mapping[Direction, int]
    tie: bool
    both_at_cap: bool
    elapsed_green_s: int
    green_service_limit_s: int
    epsilon: float | None
    policy_revision: int | None
    fixed_green_target_s: int | None = None  # fixed-time decisions only
    # Q-learning decisions only (also set on a queue fallback, which still has a state):
    q_state: "QState | None" = None
    q_values: Mapping[Direction, float] | None = None       # decision-time values, before any later update
    q_update_counts: Mapping[Direction, int] | None = None  # completed updates per action at decision time
    exploration_draw: float | None = None                   # training: the explore-or-not uniform draw
    fallback_reason: FallbackReason | None = None

    def __post_init__(self) -> None:
        _frozen_mapping(self, "scores", "counts")
        if self.q_values is not None:
            _frozen_mapping(self, "q_values", "q_update_counts")

    @property
    def retains(self) -> bool:
        return self.requested is self.current_green


# ----------------------------------------------------------------------
# Learning
# ----------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class QState:
    """(ns_queue_bin, we_queue_bin, current_green, elapsed_green_bin); exists only for green observations."""

    ns_bin: int
    we_bin: int
    green: Direction
    elapsed_bin: int

    def key(self) -> list:
        return [self.ns_bin, self.we_bin, self.green.value, self.elapsed_bin]


@dataclass(frozen=True)
class PendingFeedback:
    """One accepted training choice awaiting its next eligible boundary. Never restored across episodes.

    Samples are the observations at start+1, start+2, ...; the observation at `start_time` only
    defines the starting state.
    """

    decision_id: str
    state: QState
    action: Direction
    start_time: int
    sample_ids: tuple[str, ...] = ()
    rewards: tuple[float, ...] = ()

    @property
    def elapsed_s(self) -> int:
        return len(self.rewards)


@dataclass(frozen=True)
class LearningUpdate:
    decision_id: str
    start_time: int
    end_time: int
    sample_ids: tuple[str, ...]
    rewards: tuple[float, ...]
    state: QState
    next_state: QState
    action: Direction
    duration_s: int
    raw_reward_sum: float
    discounted_reward: float
    discount_per_second: float
    bootstrap_multiplier: float
    bootstrap_values: Mapping[Direction, float]
    bootstrap_allowed: tuple[Direction, ...]
    bootstrap_max: float
    alpha: float
    target: float
    old_q: float
    new_q: float
    update_count_before: int
    update_count_after: int
    policy_revision: int  # revision after this update

    def __post_init__(self) -> None:
        _frozen_mapping(self, "bootstrap_values")


class CompletionStatus(str, Enum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: str
    completion: CompletionStatus
    trace_complete: bool
    persistence_ok: bool
    planned_horizon_s: int
    final_valid_time_s: int | None
    failure: str | None
    counters: Mapping[str, int]
    # Training checkpoint outcome for this run, separate from simulation completion and log persistence:
    # None (no checkpointing), "saved", "failed", or "unconfirmed".
    checkpoint_status: str | None = None
    last_checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        _frozen_mapping(self, "counters")
