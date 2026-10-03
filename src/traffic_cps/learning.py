"""Tabular Q-learning core (Module 2B, Module 5 feedback, build contract Section F).

Pure arithmetic and records, no SUMO: state encoding, the Q-table with
completed-update counts and a policy revision, the per-choice feedback window,
the duration-aware update, and the exploration schedule. The runtime decides
when a window opens, receives samples, closes, or is discarded.

    R      = sum(d ** (i - 1) * r_i, i = 1..T),  r_i = -(ns_score_i + we_score_i)
    target = R + d ** T * max_allowed Q(next_state, a)
    new_Q  = old_Q + alpha * (target - old_Q),   d = gamma_5 ** (1 / 5)
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import replace
from typing import Any, Iterable

from traffic_cps.contracts import Direction, LearningUpdate, Observation, PendingFeedback, QState

ACTIONS = (Direction.NS, Direction.WE)  # fixed action order for records and checkpoints
QUEUE_FORMULA_ID = "halting_capacity_v1"
QUEUE_BINS = 5
ELAPSED_BINS = 4


class LearningError(RuntimeError):
    """Invalid learning input or record. Never clipped, defaulted, or skipped."""


def _real(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LearningError(f"{what} must be a finite number, got {value!r}")
    return float(value)


# ----------------------------------------------------------------------
# State encoding
# ----------------------------------------------------------------------

def queue_bin(score: float, edges: tuple[float, ...]) -> int:
    """Half-open bins [e0, e1), ..., with the last bin closed at the cap: [1.6, 2.0]."""
    score = _real(score, "queue score")
    if score < edges[0] or score > edges[-1]:
        raise LearningError(f"queue score {score!r} is outside [{edges[0]}, {edges[-1]}]")
    return min(bisect_right(edges, score) - 1, len(edges) - 2)


def elapsed_bin(elapsed_s: float, edges: tuple[float, ...]) -> int:
    """Bins [0, 5), [5, 32.5), [32.5, 60), [60, infinity)."""
    elapsed_s = _real(elapsed_s, "elapsed green")
    if elapsed_s < edges[0]:
        raise LearningError(f"elapsed green {elapsed_s!r} is negative")
    return bisect_right(edges, elapsed_s) - 1


def encode_state(o: Observation, queue_edges: tuple[float, ...], elapsed_edges: tuple[float, ...]) -> QState:
    if o.current_green is None or o.elapsed_green_s is None:
        raise LearningError(f"observation {o.observation_id} is not a green observation; it has no Q-state")
    return QState(queue_bin(o.scores[Direction.NS], queue_edges), queue_bin(o.scores[Direction.WE], queue_edges),
                  o.current_green, elapsed_bin(o.elapsed_green_s, elapsed_edges))


def state_from_key(key: Any) -> QState:
    if not isinstance(key, list) or len(key) != 4:
        raise LearningError(f"state key must be a 4-item list, got {key!r}")
    ns, we, green, eb = key
    for v, n, what in ((ns, QUEUE_BINS, "ns bin"), (we, QUEUE_BINS, "we bin"), (eb, ELAPSED_BINS, "elapsed bin")):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < n:
            raise LearningError(f"state {key!r}: invalid {what}")
    try:
        return QState(ns, we, Direction(green), eb)
    except ValueError as exc:
        raise LearningError(f"state {key!r}: invalid green direction") from exc


# ----------------------------------------------------------------------
# Q-table
# ----------------------------------------------------------------------

class QTable:
    """State -> per-action value and completed-update count. Missing entries read as 0 / 0."""

    def __init__(self) -> None:
        self._values: dict[QState, dict[Direction, float]] = {}
        self._counts: dict[QState, dict[Direction, int]] = {}
        self.revision = 0  # number of completed updates applied since the table was created

    def __len__(self) -> int:
        return len(self._values)

    def contains(self, state: QState) -> bool:
        return state in self._values

    def states(self) -> list[QState]:
        return sorted(self._values)

    def values(self, state: QState) -> dict[Direction, float]:
        return dict(self._values.get(state, {a: 0.0 for a in ACTIONS}))

    def update_counts(self, state: QState) -> dict[Direction, int]:
        return dict(self._counts.get(state, {a: 0 for a in ACTIONS}))

    def ensure(self, state: QState) -> None:
        """Training only: an encountered state starts at zero values with zero completed updates."""
        if state not in self._values:
            self._values[state] = {a: 0.0 for a in ACTIONS}
            self._counts[state] = {a: 0 for a in ACTIONS}

    def _set(self, state: QState, action: Direction, value: float, count: int) -> None:
        self.ensure(state)
        self._values[state][action] = value
        self._counts[state][action] = count

    def to_json(self) -> dict:
        return {
            "revision": self.revision,
            "action_order": [a.value for a in ACTIONS],
            "entries": [{"state": s.key(),
                         "values": {a.value: self._values[s][a] for a in ACTIONS},
                         "counts": {a.value: self._counts[s][a] for a in ACTIONS}} for s in self.states()],
        }

    @classmethod
    def from_json(cls, data: Any) -> "QTable":
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise LearningError("Q-table JSON must be an object with an entries list")
        table = cls()
        revision = data.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise LearningError(f"Q-table revision must be a nonnegative integer, got {revision!r}")
        for entry in data["entries"]:
            if not isinstance(entry, dict):
                raise LearningError(f"Q-table entry must be an object, got {entry!r}")
            state = state_from_key(entry.get("state"))
            if table.contains(state):
                raise LearningError(f"duplicate Q-table state {state.key()}")
            values, counts = entry.get("values"), entry.get("counts")
            if not isinstance(values, dict) or not isinstance(counts, dict) \
                    or set(values) != {a.value for a in ACTIONS} or set(counts) != {a.value for a in ACTIONS}:
                raise LearningError(f"state {state.key()}: values and counts need exactly {[a.value for a in ACTIONS]}")
            table.ensure(state)
            for a in ACTIONS:
                count = counts[a.value]
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise LearningError(f"state {state.key()}: invalid update count {count!r}")
                table._set(state, a, _real(values[a.value], f"state {state.key()} value"), count)
        table.revision = revision
        return table

    def copy(self) -> "QTable":
        return QTable.from_json(self.to_json())


# ----------------------------------------------------------------------
# Feedback window and update
# ----------------------------------------------------------------------

def open_interval(decision_id: str, state: QState, action: Direction, start_time: int) -> PendingFeedback:
    return PendingFeedback(decision_id=decision_id, state=state, action=action, start_time=start_time)


def add_sample(w: PendingFeedback, o: Observation) -> PendingFeedback:
    """Add the one-second reward of the next observation; exactly once per second, from start+1."""
    expected = w.start_time + w.elapsed_s + 1
    if o.time != expected:
        raise LearningError(f"feedback for {w.decision_id}: expected a sample at t={expected}, got t={o.time}")
    reward = -(_real(o.scores[Direction.NS], "NS score") + _real(o.scores[Direction.WE], "WE score"))
    return replace(w, sample_ids=(*w.sample_ids, o.observation_id), rewards=(*w.rewards, reward))


def discount_per_second(discount_factor: float, unit_s: int) -> float:
    return discount_factor ** (1.0 / unit_s)


def close_interval(table: QTable, w: PendingFeedback, next_state: QState, allowed: Iterable[Direction],
                   end_time: int, alpha: float, d: float) -> LearningUpdate:
    """Compute the update from values as they stand, then apply it. Initializes the bootstrap entry."""
    if not w.rewards:
        raise LearningError(f"feedback for {w.decision_id} has no reward samples")
    if w.start_time + w.elapsed_s != end_time:
        raise LearningError(f"feedback for {w.decision_id} ends at t={w.start_time + w.elapsed_s}, "
                            f"not at the boundary t={end_time}")
    allowed = tuple(allowed)
    if not allowed or any(a not in ACTIONS for a in allowed):
        raise LearningError(f"invalid allowed actions {allowed!r}")

    table.ensure(next_state)
    table.ensure(w.state)
    # Read every target input before writing the original entry.
    bootstrap = table.values(next_state)
    old_q = table.values(w.state)[w.action]
    count_before = table.update_counts(w.state)[w.action]

    T = w.elapsed_s
    discounted = 0.0
    for i, r in enumerate(w.rewards):
        discounted += d ** i * r
    multiplier = d ** T
    best = max(bootstrap[a] for a in allowed)
    target = discounted + multiplier * best
    new_q = old_q + alpha * (target - old_q)
    if not math.isfinite(new_q):
        raise LearningError(f"update for {w.decision_id} produced a nonfinite value")

    table._set(w.state, w.action, new_q, count_before + 1)
    table.revision += 1
    return LearningUpdate(
        decision_id=w.decision_id, start_time=w.start_time, end_time=end_time, sample_ids=w.sample_ids,
        rewards=w.rewards, state=w.state, next_state=next_state, action=w.action, duration_s=T,
        raw_reward_sum=math.fsum(w.rewards), discounted_reward=discounted, discount_per_second=d,
        bootstrap_multiplier=multiplier, bootstrap_values=bootstrap, bootstrap_allowed=allowed,
        bootstrap_max=best, alpha=alpha, target=target, old_q=old_q, new_q=new_q,
        update_count_before=count_before, update_count_after=count_before + 1, policy_revision=table.revision,
    )


# ----------------------------------------------------------------------
# Exploration schedule and learner state
# ----------------------------------------------------------------------

class ExplorationSchedule:
    """epsilon <- max(floor, epsilon * decay) after each accepted eligible training choice."""

    def __init__(self, initial: float, floor: float, decay: float, accepted_choices: int = 0):
        self.epsilon = initial
        self.floor = floor
        self.decay = decay
        self.accepted_choices = accepted_choices

    def accepted(self) -> None:
        self.accepted_choices += 1
        self.epsilon = max(self.floor, self.epsilon * self.decay)

    def to_json(self) -> dict:
        return {"epsilon": self.epsilon, "floor": self.floor, "decay": self.decay,
                "accepted_choices": self.accepted_choices}

    @classmethod
    def from_json(cls, data: dict) -> "ExplorationSchedule":
        return cls(data["epsilon"], data["floor"], data["decay"], data["accepted_choices"])


class Learner:
    """Everything that persists across training episodes: table, exploration progress, controller RNG.

    Pending feedback is deliberately not here; it belongs to one episode's runtime.
    """

    def __init__(self, config, table: QTable | None = None, schedule: ExplorationSchedule | None = None,
                 rng: random.Random | None = None):
        lc = config.learning
        self.alpha = lc.alpha
        self.d = discount_per_second(lc.discount_factor, lc.discount_unit_s)
        self.queue_edges = tuple(lc.queue_edges)
        self.elapsed_edges = tuple(lc.elapsed_edges_s)
        self.table = table if table is not None else QTable()
        self.schedule = schedule or ExplorationSchedule(lc.epsilon_initial, lc.epsilon_floor, lc.epsilon_decay)
        self.rng = rng if rng is not None else random.Random(lc.controller_seed)
        self.completed_updates = 0

    def encode(self, o: Observation) -> QState:
        return encode_state(o, self.queue_edges, self.elapsed_edges)

    def close(self, w: PendingFeedback, next_state: QState, end_time: int) -> LearningUpdate:
        update = close_interval(self.table, w, next_state, ACTIONS, end_time, self.alpha, self.d)
        self.completed_updates += 1
        return update

    def snapshot(self) -> dict:
        """The full starting policy for policy_start.json (no pending feedback, by design)."""
        return {
            "kind": "q_table",
            "queue_formula_id": QUEUE_FORMULA_ID,
            "queue_edges": list(self.queue_edges),
            "elapsed_edges_s": list(self.elapsed_edges),
            "alpha": self.alpha,
            "discount_per_second": self.d,
            "exploration": self.schedule.to_json(),
            "table": self.table.to_json(),
        }
