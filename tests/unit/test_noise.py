"""Section H robustness wrapper and Module 8 phantom/masked demand, without SUMO."""

import math
import random

import pytest

from traffic_cps.faults import SensorNoise, service_demand_fault

LANES = ("N_J_0", "N_J_1", "W_J_0", "W_J_1")
TRUE = {"N_J_0": 5, "N_J_1": 0, "W_J_0": 2, "W_J_1": 9}


def run(noise, seconds=50, true=TRUE):
    return [noise.measure(t, dict(true)) for t in range(seconds)]


def test_sectionH_time_zero_delivers_truth():
    n = SensorNoise("high", 3, 0.2, seed=10_200, lane_order=LANES)
    assert n.measure(0, dict(TRUE)) == TRUE


def test_sectionH_two_draws_per_lane_per_second_in_fixed_lane_order():
    n = SensorNoise("low", 1, 0.05, seed=7, lane_order=LANES)
    ref = random.Random(7)
    n.measure(0, dict(TRUE))                                   # t=0 consumes nothing
    out = n.measure(1, dict(TRUE))
    for lane in LANES:
        u_stale, u_noise = ref.random(), ref.random()
        expected_noise = math.floor(u_noise * 3) - 1
        expected = TRUE[lane] if u_stale < 0.05 else max(0, TRUE[lane] + expected_noise)  # previous == truth at t=0
        assert out[lane] == expected
    assert n.draws == 8


def test_sectionH_both_levels_consume_identical_draw_sequences():
    low = SensorNoise("low", 1, 0.05, seed=10_201, lane_order=LANES)
    high = SensorNoise("high", 3, 0.20, seed=10_201, lane_order=LANES)
    run(low), run(high)
    assert low.draws == high.draws == 49 * 4 * 2
    assert low.rng.getstate() == high.rng.getstate()


def test_sectionH_noise_bounded_nonnegative_and_stale_reuses_previous_delivery():
    n = SensorNoise("high", 3, 0.2, seed=3, lane_order=LANES)
    out = run(n, 400)
    for t in range(1, 400):
        for lane in LANES:
            value = out[t][lane]
            # fresh readings are truth +-3 clamped at 0; anything else must be a stale repeat
            assert value >= 0
            assert value == out[t - 1][lane] or value == max(0, value) and abs(value - TRUE[lane]) <= 3 \
                or (value == 0 and TRUE[lane] <= 3)
    assert any(out[t]["N_J_1"] > 0 for t in range(1, 400))     # true 0 can be measured positive (phantom source)


def test_sectionH_noise_values_cover_the_full_symmetric_range():
    n = SensorNoise("high", 3, 0.0, seed=11, lane_order=LANES)
    seen = {out["W_J_1"] - 9 for out in run(n, 500)[1:]}
    assert seen == {-3, -2, -1, 0, 1, 2, 3}


def test_sectionH_seconds_must_be_consecutive():
    n = SensorNoise("low", 1, 0.05, seed=1, lane_order=LANES)
    n.measure(0, dict(TRUE))
    with pytest.raises(ValueError, match="t=1"):
        n.measure(2, dict(TRUE))


@pytest.mark.parametrize("bound,p", [(-1, 0.1), (1, -0.1), (1, 1.5)])
def test_sectionH_invalid_levels_rejected(bound, p):
    with pytest.raises(ValueError):
        SensorNoise("bad", bound, p, seed=0, lane_order=LANES)


# Phantom / masked classification at the service limit -------------------------------------

@pytest.mark.parametrize("true_c,measured_c,elapsed,forced,expected", [
    (0, 1, 60, True, "phantom_demand"),      # forced for a vehicle that was not there
    (2, 1, 60, True, None),                  # forced, and demand was real
    (3, 0, 60, False, "masked_demand"),      # real demand hidden at the limit: no switch
    (3, 0, 75, False, "masked_demand"),
    (3, 0, 59, False, None),                 # below the limit: no contract obligation yet
    (0, 0, 70, False, None),                 # no demand, none reported
])
def test_module8_phantom_and_masked_demand_detected_on_known_cases(true_c, measured_c, elapsed, forced, expected):
    assert service_demand_fault(true_c, measured_c, elapsed, 60, forced) == expected
