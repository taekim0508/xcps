"""Module 1 (sensor): counts, scores, validation, and the same-second retry policy."""

import pytest

from traffic_cps.contracts import (
    Direction,
    RawSnapshot,
    SignalColor,
    SignalPhase,
    SignalTiming,
)
from traffic_cps.sensor import SensorFailure, TrafficSensor

LANES = {Direction.NS: ("N_J_0", "N_J_1"), Direction.WE: ("W_J_0", "W_J_1")}
# Generated lengths from scenarios/single (chunk 0).
LENGTHS = {"N_J_0": 246.0, "N_J_1": 246.0, "W_J_0": 239.6, "W_J_1": 239.6}


def make_sensor(retries=2):
    return TrafficSensor(LANES, LENGTHS, vehicle_length_m=5.0, minimum_gap_m=2.5, retries=retries)


def raw(time=7, counts=None, signal="GGrr"):
    counts = counts if counts is not None else {"N_J_0": 10, "N_J_1": 3, "W_J_0": 5, "W_J_1": 40}
    return RawSnapshot(time, signal, counts, (), (), (), ())


def timing(time=7):
    return SignalTiming(
        time=time, mask="GGrr", phase=SignalPhase.NS_GREEN, current_green=Direction.NS, green_onset_s=0,
        transition_target=None, transition_start_s=None,
        colors={Direction.NS: SignalColor.GREEN, Direction.WE: SignalColor.RED},
        color_since_s={Direction.NS: 0, Direction.WE: 0},
    )


def test_module1_capacity_uses_generated_length_over_7_5():
    sensor = make_sensor()
    assert sensor.capacity["N_J_0"] == pytest.approx(32.8, rel=1e-12)       # 246.0 / 7.5
    assert sensor.capacity["W_J_0"] == pytest.approx(31.946666666666666, rel=1e-12)  # 239.6 / 7.5


def test_module1_totals_equal_lane_sums_and_scores_equal_normalized_sums():
    sensor = make_sensor()
    snapshot, quality = sensor.read(lambda: raw(), expected_time=7)
    obs = sensor.observe(snapshot, timing(), quality)
    assert obs.counts_by_direction == {Direction.NS: 13, Direction.WE: 45}
    # Hand-computed: 10/32.8 + 3/32.8 = 0.39634..., 5/31.9466... + min(1, 40/31.9466...) = 0.15651... + 1.0
    assert obs.lane_scores["N_J_0"] == pytest.approx(10 / 32.8, rel=1e-12)
    assert obs.lane_scores["W_J_1"] == 1.0  # 40 vehicles exceed estimated capacity: capped
    assert obs.scores[Direction.NS] == pytest.approx(13 / 32.8, rel=1e-12)
    assert obs.scores[Direction.WE] == pytest.approx(5 / (239.6 / 7.5) + 1.0, rel=1e-12)
    # Raw counts are preserved, not reconstructed from scores.
    assert obs.counts_by_lane["W_J_1"] == 40


def test_module1_counts_scores_colors_share_one_timestamp():
    sensor = make_sensor()
    snapshot, quality = sensor.read(lambda: raw(time=7), expected_time=7)
    obs = sensor.observe(snapshot, timing(7), quality)
    assert obs.time == 7
    assert obs.elapsed_green_s == 7
    assert obs.raw_signal == "GGrr"
    with pytest.raises(SensorFailure, match="timing"):
        sensor.observe(snapshot, timing(8), quality)


def test_module1_zero_counts_are_legitimate():
    sensor = make_sensor()
    zeros = {"N_J_0": 0, "N_J_1": 0, "W_J_0": 0, "W_J_1": 0}
    snapshot, quality = sensor.read(lambda: raw(counts=zeros), expected_time=7)
    obs = sensor.observe(snapshot, timing(), quality)
    assert obs.scores == {Direction.NS: 0.0, Direction.WE: 0.0}
    assert quality.failures == ()


@pytest.mark.parametrize(
    "counts,field",
    [
        ({"N_J_0": 1, "N_J_1": 1, "W_J_0": 1}, "W_J_1"),                    # missing reading
        ({"N_J_0": -1, "N_J_1": 1, "W_J_0": 1, "W_J_1": 1}, "N_J_0"),       # negative
        ({"N_J_0": 1.5, "N_J_1": 1, "W_J_0": 1, "W_J_1": 1}, "N_J_0"),      # non-integer
        ({"N_J_0": True, "N_J_1": 1, "W_J_0": 1, "W_J_1": 1}, "N_J_0"),     # bool is not a count
        ({"N_J_0": None, "N_J_1": 1, "W_J_0": 1, "W_J_1": 1}, "N_J_0"),     # None is not zero
    ],
)
def test_module1_missing_or_invalid_reading_is_not_a_zero(counts, field):
    sensor = make_sensor(retries=0)
    with pytest.raises(SensorFailure) as info:
        sensor.read(lambda: raw(counts=counts), expected_time=7)
    assert info.value.failures[0].field == field


def test_module1_transient_failure_recovers_within_two_retries():
    calls = []
    notices = []

    def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise ConnectionError("lane read failed")
        return raw()

    snapshot, quality = make_sensor().read(flaky, expected_time=7, notify=notices.append)
    assert len(calls) == 3
    assert quality.attempts == 3
    assert [f.attempt for f in quality.failures] == [1, 2]
    assert all(f.time == 7 for f in quality.failures)
    assert len(notices) == 2  # the user is told each time a retry happens
    assert snapshot.time == 7


def test_module1_persistent_failure_stops_after_three_attempts():
    calls = []

    def broken():
        calls.append(1)
        raise ConnectionError("lane read failed")

    with pytest.raises(SensorFailure) as info:
        make_sensor().read(broken, expected_time=7)
    assert len(calls) == 3  # initial attempt + 2 retries, never more
    assert [f.attempt for f in info.value.failures] == [1, 2, 3]


def test_module1_time_change_during_retrieval_is_failure():
    with pytest.raises(SensorFailure, match="time"):
        make_sensor(retries=0).read(lambda: raw(time=8), expected_time=7)


def test_module1_rejects_nonpositive_lane_length():
    with pytest.raises(ValueError):
        TrafficSensor(LANES, {**LENGTHS, "N_J_0": 0.0}, 5.0, 2.5, 2)
