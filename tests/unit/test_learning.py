"""Module 2B and Module 5 learning contract: bins, table, feedback window, update arithmetic, epsilon.

Expected numbers are hand-computed with d = 0.95 ** (1/5) = 0.9897937816869885 (so d**5 = 0.95).
"""

import math

import pytest

from traffic_cps.contracts import Direction, QState
from traffic_cps.learning import (
    ExplorationSchedule,
    LearningError,
    QTable,
    close_interval,
    discount_per_second,
    elapsed_bin,
    encode_state,
    open_interval,
    add_sample,
    queue_bin,
)
from tests.unit.factories import observation

NS, WE = Direction.NS, Direction.WE
QE = (0.0, 0.4, 0.8, 1.2, 1.6, 2.0)
EE = (0.0, 5.0, 32.5, 60.0)
D = 0.9897937816869885


# ----------------------------------------------------------------------
# Bins
# ----------------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (0.0, 0), (0.3999999, 0), (0.4, 1), (0.7999, 1), (0.8, 2), (1.2, 3), (1.5999, 3), (1.6, 4), (2.0, 4),
])
def test_module2B_queue_bin_edges(score, expected):
    assert queue_bin(score, QE) == expected


@pytest.mark.parametrize("score", [-0.01, 2.0000001, math.nan, math.inf, -math.inf, None, True])
def test_module2B_queue_bin_rejects_invalid_instead_of_clipping(score):
    with pytest.raises(LearningError):
        queue_bin(score, QE)


@pytest.mark.parametrize("elapsed,expected", [
    (0, 0), (4, 0), (5, 1), (32, 1), (32.5, 2), (33, 2), (59, 2), (60, 3), (900, 3),
])
def test_module2B_elapsed_bin_edges(elapsed, expected):
    assert elapsed_bin(elapsed, EE) == expected


@pytest.mark.parametrize("elapsed", [-1, math.nan, None])
def test_module2B_elapsed_bin_rejects_invalid(elapsed):
    with pytest.raises(LearningError):
        elapsed_bin(elapsed, EE)


def test_module2B_encode_state_uses_raw_scores_green_and_elapsed():
    o = observation(ns_score=0.41, we_score=1.61, green=WE, elapsed=33)
    assert encode_state(o, QE, EE) == QState(1, 4, WE, 2)


def test_module2B_yellow_observation_has_no_state():
    with pytest.raises(LearningError, match="no Q-state"):
        encode_state(observation(target=WE), QE, EE)


# ----------------------------------------------------------------------
# Table
# ----------------------------------------------------------------------

def test_module2B_missing_entries_read_as_zero_with_zero_count_without_insertion():
    t = QTable()
    s = QState(0, 0, NS, 1)
    assert t.values(s) == {NS: 0.0, WE: 0.0} and t.update_counts(s) == {NS: 0, WE: 0}
    assert not t.contains(s) and len(t) == 0


def test_module2B_training_insert_initializes_zero_entries():
    t = QTable()
    s = QState(1, 0, WE, 2)
    t.ensure(s)
    assert t.contains(s) and t.values(s) == {NS: 0.0, WE: 0.0} and t.revision == 0


def test_module2B_table_json_round_trip_keeps_zero_entries_and_revision():
    t = QTable()
    t.ensure(QState(0, 0, NS, 1))
    t.ensure(QState(1, 2, WE, 3))
    t._set(QState(1, 2, WE, 3), WE, -1.25, 4)
    t.revision = 4
    back = QTable.from_json(t.to_json())
    assert back.to_json() == t.to_json() and len(back) == 2 and back.revision == 4


@pytest.mark.parametrize("bad", [
    {"revision": 0, "entries": [{"state": [0, 0, "NS", 1], "values": {"NS": "x", "WE": 0}, "counts": {"NS": 0, "WE": 0}}]},
    {"revision": 0, "entries": [{"state": [0, 0, "NS", 1], "values": {"NS": 0, "WE": 0}, "counts": {"NS": -1, "WE": 0}}]},
    {"revision": 0, "entries": [{"state": [9, 0, "NS", 1], "values": {"NS": 0, "WE": 0}, "counts": {"NS": 0, "WE": 0}}]},
    {"revision": 0, "entries": [{"state": [0, 0, "NS", 1], "values": {"NS": 0, "WE": 0}, "counts": {"NS": 0, "WE": 0}}] * 2},
])
def test_module2B_table_json_rejects_malformed(bad):
    with pytest.raises(LearningError):
        QTable.from_json(bad)


# ----------------------------------------------------------------------
# Feedback window: one reward per second, from t+1
# ----------------------------------------------------------------------

def window():
    return open_interval("dec-10", QState(0, 0, NS, 1), WE, 10)


def test_module5_reward_is_negative_summed_score_added_once_per_second():
    w = window()
    w = add_sample(w, observation(time=11, ns_score=0.25, we_score=0.5))
    w = add_sample(w, observation(time=12, ns_score=0.0, we_score=0.125))
    assert w.rewards == (-0.75, -0.125) and w.sample_ids == ("obs-11", "obs-12") and w.elapsed_s == 2


def test_module5_start_observation_is_not_a_reward_sample():
    with pytest.raises(LearningError, match="expected a sample at t=11"):
        add_sample(window(), observation(time=10))


def test_module5_same_second_sample_is_never_counted_twice():
    w = add_sample(window(), observation(time=11))
    with pytest.raises(LearningError, match="expected a sample at t=12"):
        add_sample(w, observation(time=11))
    with pytest.raises(LearningError, match="expected a sample at t=12"):
        add_sample(w, observation(time=13))   # a skipped second is also an error


# ----------------------------------------------------------------------
# Update arithmetic (hand-computed)
# ----------------------------------------------------------------------

def filled(rewards, start=10):
    w = open_interval("dec-10", QState(0, 0, NS, 1), WE, start)
    for i, r in enumerate(rewards, start=1):
        w = add_sample(w, observation(time=start + i, ns_score=-r, we_score=0.0))
    return w


def test_module5_discount_per_second_is_fifth_root():
    assert discount_per_second(0.95, 5) == pytest.approx(D, abs=1e-15)
    assert discount_per_second(0.95, 5) ** 5 == pytest.approx(0.95, abs=1e-15)


def test_module5_hand_computed_interval_update():
    # R = -0.2 - 0.4d - 0.4d^2 - 0.6d^3 - 0.8d^4 = -2.3374465834155274
    # target = R + d^5 * max(-2.0, -3.0) = R - 1.9 = -4.237446583415528
    # new = -1.0 + 0.1 * (target + 1.0) = -1.3237446583415529
    t = QTable()
    s, nxt = QState(0, 0, NS, 1), QState(1, 0, WE, 1)
    t.ensure(s)
    t._set(s, WE, -1.0, 3)
    t.ensure(nxt)
    t._set(nxt, NS, -2.0, 1)
    t._set(nxt, WE, -3.0, 1)
    t.revision = 7
    u = close_interval(t, filled([-0.2, -0.4, -0.4, -0.6, -0.8]), nxt, (NS, WE), 15, alpha=0.1, d=D)
    assert u.duration_s == 5 and u.end_time == 15 and u.start_time == 10
    assert u.raw_reward_sum == pytest.approx(-2.4, abs=1e-12)
    assert u.discounted_reward == pytest.approx(-2.3374465834155274, abs=1e-12)
    assert u.bootstrap_multiplier == pytest.approx(0.95, abs=1e-12)
    assert u.bootstrap_values == {NS: -2.0, WE: -3.0} and u.bootstrap_max == -2.0
    assert u.target == pytest.approx(-4.237446583415528, abs=1e-12)
    assert u.old_q == -1.0 and u.new_q == pytest.approx(-1.3237446583415529, abs=1e-12)
    assert (u.update_count_before, u.update_count_after, u.policy_revision) == (3, 4, 8)
    # applied to the table:
    assert t.values(s)[WE] == u.new_q and t.update_counts(s)[WE] == 4 and t.revision == 8
    assert t.values(s)[NS] == 0.0 and t.update_counts(s)[NS] == 0       # other action untouched


def test_module5_ten_second_interval_update_from_zero():
    # Accepted at 10, closed at 20 (the 10 -> 12 -> 20 example): T = 10, every r = -0.5.
    # R = -0.5 * (1 - d^10) / (1 - d) = -4.776499826370615; bootstrap 0; new = 0.1 * R.
    t = QTable()
    nxt = QState(0, 0, WE, 1)
    u = close_interval(t, filled([-0.5] * 10), nxt, (NS, WE), 20, alpha=0.1, d=D)
    assert u.duration_s == 10 and u.sample_ids == tuple(f"obs-{i}" for i in range(11, 21))
    assert u.discounted_reward == pytest.approx(-4.776499826370615, abs=1e-12)
    assert u.new_q == pytest.approx(-0.4776499826370615, abs=1e-12)
    assert t.contains(nxt)                                               # bootstrap entry initialized in training


def test_module5_target_reads_values_before_writing_same_entry():
    # next_state == state: the bootstrap must use the old value of the entry being updated.
    t = QTable()
    s = QState(0, 0, NS, 1)
    t.ensure(s)
    t._set(s, NS, -10.0, 2)
    t._set(s, WE, -1.0, 2)
    w = open_interval("dec-5", s, NS, 5)
    w = add_sample(w, observation(time=6, ns_score=0.0, we_score=0.0))
    u = close_interval(t, w, s, (NS, WE), 6, alpha=0.1, d=D)
    assert u.bootstrap_values == {NS: -10.0, WE: -1.0} and u.bootstrap_max == -1.0
    assert u.target == pytest.approx(D * -1.0, abs=1e-15)


def test_module5_closing_requires_samples_through_the_boundary():
    t = QTable()
    with pytest.raises(LearningError, match="no reward samples"):
        close_interval(t, window(), QState(0, 0, NS, 1), (NS, WE), 10, alpha=0.1, d=D)
    with pytest.raises(LearningError, match="ends at"):
        close_interval(t, filled([-0.1, -0.1]), QState(0, 0, NS, 1), (NS, WE), 15, alpha=0.1, d=D)
    assert t.revision == 0 and len(t) == 0


def test_module2B_counts_change_only_on_completed_updates():
    t = QTable()
    s = QState(0, 0, NS, 1)
    t.ensure(s)
    open_interval("dec-10", s, WE, 10)          # an accepted choice alone changes nothing
    assert t.update_counts(s) == {NS: 0, WE: 0} and t.revision == 0


# ----------------------------------------------------------------------
# Exploration schedule
# ----------------------------------------------------------------------

def test_module2B_epsilon_decays_per_accepted_choice_to_floor_after_4605():
    e = ExplorationSchedule(initial=1.0, floor=0.1, decay=0.9995)
    e.accepted()
    assert e.epsilon == 0.9995 and e.accepted_choices == 1
    for _ in range(4603):
        e.accepted()
    assert e.accepted_choices == 4604 and e.epsilon > 0.1
    e.accepted()
    assert e.accepted_choices == 4605 and e.epsilon == 0.1
    e.accepted()
    assert e.epsilon == 0.1


def test_module2B_epsilon_schedule_round_trips():
    e = ExplorationSchedule(initial=1.0, floor=0.1, decay=0.9995)
    for _ in range(3):
        e.accepted()
    back = ExplorationSchedule.from_json(e.to_json())
    assert back.epsilon == e.epsilon and back.accepted_choices == 3
