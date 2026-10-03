"""Module 4A (safety monitor): invariants S1 to S4 on hand-built signal histories."""

from types import SimpleNamespace

import pytest

from traffic_cps.contracts import Direction, Origin, OriginKind
from traffic_cps.safety import SafetyMonitor

NS, WE = Direction.NS, Direction.WE
INIT = Origin(OriginKind.INITIALIZATION, "init")
CTRL = Origin(OriginKind.CONTROLLER_DECISION, "dec-1")
DONE = Origin(OriginKind.TRANSITION_COMPLETION, "dec-1")


def monitor(horizon=3600):
    return SafetyMonitor(
        index_directions=(NS, NS, WE, WE), yellow_s=2, minimum_green_s=5,
        green_service_limit_s=60, horizon_s=horizon, initial_green=NS,
    )


def started(horizon=3600):
    m = monitor(horizon)
    assert m.check_proposed("GGrr", 0, INIT).allowed
    assert m.observe("GGrr", 0, "write") == []
    return m


def ticks(m, mask, start, end):
    """Observe an unchanged mask for t in [start, end]; return all violations."""
    found = []
    for t in range(start, end + 1):
        found += m.observe(mask, t, "tick")
    return found


def obs(time, ns, we):
    return SimpleNamespace(time=time, counts_by_direction={NS: ns, WE: we})


# --- startup exception ----------------------------------------------------

def test_module4A_startup_exception_only_once_at_time_zero():
    m = started()
    assert not m.check_proposed("GGrr", 0, INIT).allowed
    late = monitor()
    verdict = late.check_proposed("GGrr", 1, INIT)
    assert not verdict.allowed and verdict.invariant == "S2"


def test_module4A_startup_must_be_configured_direction():
    assert not monitor().check_proposed("rrGG", 0, INIT).allowed


def test_module4A_no_write_before_startup():
    assert not monitor().check_proposed("yyrr", 0, CTRL).allowed


# --- normal transition passes --------------------------------------------

def test_module4A_normal_switch_has_no_overrides_or_violations():
    m = started()
    assert ticks(m, "GGrr", 1, 10) == []
    assert m.check_proposed("yyrr", 10, CTRL).allowed
    assert m.observe("yyrr", 10, "write") == []
    assert ticks(m, "yyrr", 11, 12) == []
    assert m.check_proposed("rrGG", 12, DONE).allowed
    assert m.observe("rrGG", 12, "write") == []
    assert m.overrides == [] and m.violations == []


# --- pre-command vetoes ---------------------------------------------------

def test_module4A_premature_yellow_vetoed_S3_with_linked_override():
    m = started()
    ticks(m, "GGrr", 1, 3)
    verdict = m.check_proposed("yyrr", 3, CTRL)
    assert not verdict.allowed and verdict.invariant == "S3"
    assert m.overrides[0].origin == CTRL and m.overrides[0].invariant == "S3"
    assert m.overrides[0].proposed_mask == "yyrr" and m.overrides[0].current_mask == "GGrr"


def test_module4A_direct_opposing_green_vetoed_S2():
    m = started()
    ticks(m, "GGrr", 1, 10)
    verdict = m.check_proposed("rrGG", 10, CTRL)
    assert not verdict.allowed and verdict.invariant == "S2"


def test_module4A_both_directions_non_red_vetoed_S1():
    m = started()
    ticks(m, "GGrr", 1, 10)
    for mask in ("GGGG", "GGyy", "yyyy"):
        verdict = m.check_proposed(mask, 10, CTRL)
        assert not verdict.allowed and verdict.invariant == "S1", mask


def test_module4A_green_after_short_yellow_vetoed_S2():
    m = started()
    ticks(m, "GGrr", 1, 10)
    m.observe("yyrr", 10, "write")
    m.observe("yyrr", 11, "tick")
    verdict = m.check_proposed("rrGG", 11, DONE)
    assert not verdict.allowed and verdict.invariant == "S2"


def test_module4A_green_ending_without_yellow_vetoed():
    m = started()
    ticks(m, "GGrr", 1, 10)
    assert not m.check_proposed("rrrr", 10, CTRL).allowed


@pytest.mark.parametrize("mask", ["Gyrr", "GGr", "GGrrr", "GGxr", "GgrR"])
def test_module4A_invalid_or_mixed_mask_vetoed(mask):
    m = started()
    verdict = m.check_proposed(mask, 10, CTRL)
    assert not verdict.allowed and verdict.invariant == "MASK"


# --- post-observation violations -----------------------------------------

def test_module4A_observed_direct_green_to_green_is_violation_with_raw_states():
    m = started()
    ticks(m, "GGrr", 1, 10)
    found = m.observe("rrGG", 10, "write")
    assert [v.invariant for v in found] == ["S2"]
    assert found[0].raw_signal == "rrGG" and found[0].previous_raw_signal == "GGrr"
    assert m.violations == found


def test_module4A_observed_both_green_is_S1_violation():
    m = started()
    found = m.observe("GGGG", 1, "tick")
    assert "S1" in [v.invariant for v in found]


def test_module4A_observed_premature_yellow_is_S3_violation():
    m = started()
    ticks(m, "GGrr", 1, 3)
    assert [v.invariant for v in m.observe("yyrr", 3, "write")] == ["S3"]


def test_module4A_observed_invalid_mask_is_violation():
    m = started()
    assert [v.invariant for v in m.observe("GGr", 1, "tick")] == ["MASK"]


def test_module4A_monitor_uses_its_own_history_not_caller_claims():
    """Green onset comes from observed raw changes: a mask seen only at t=8 has elapsed 0 there."""
    m = monitor()
    m.check_proposed("GGrr", 0, INIT)
    m.observe("GGrr", 0, "write")
    ticks(m, "GGrr", 1, 10)
    m.observe("yyrr", 10, "write")
    ticks(m, "yyrr", 11, 12)
    m.observe("rrGG", 12, "write")
    # WE green since 12 (from raw history): yellow at 15 is premature even though NS was green long before.
    assert not m.check_proposed("rryy", 15, CTRL).allowed
    assert m.check_proposed("rryy", 17, CTRL).allowed


# --- S4 service deadline -------------------------------------------------

def test_module4A_S4_late_forced_switch_flagged_next_second():
    m = started()
    ticks(m, "GGrr", 1, 60)
    assert m.check_service_deadline(obs(60, 0, 3)) == []          # due now; deadline is t+1
    ticks(m, "GGrr", 61, 61)
    found = m.check_service_deadline(obs(61, 0, 3))
    assert [v.invariant for v in found] == ["S4"]


def test_module4A_S4_timely_switch_passes():
    m = started()
    ticks(m, "GGrr", 1, 60)
    m.check_service_deadline(obs(60, 0, 3))
    m.observe("yyrr", 61, "write")
    assert m.check_service_deadline(obs(61, 0, 3)) == []
    assert m.violations == []


def test_module4A_S4_no_demand_no_forced_switch_then_demand_appears():
    m = started()
    ticks(m, "GGrr", 1, 70)
    assert m.check_service_deadline(obs(60, 0, 0)) == []
    assert m.check_service_deadline(obs(65, 0, 0)) == []
    assert m.check_service_deadline(obs(70, 0, 1)) == []           # demand first seen at 70
    ticks(m, "GGrr", 71, 71)
    assert [v.invariant for v in m.check_service_deadline(obs(71, 0, 1))] == ["S4"]


def test_module4A_S4_suppressed_at_horizon():
    m = started(horizon=60)
    ticks(m, "GGrr", 1, 60)
    assert m.check_service_deadline(obs(60, 0, 5)) == []


def test_module4A_S4_uses_measured_competing_demand_only():
    m = started()
    ticks(m, "GGrr", 1, 61)
    # Heavy demand on the green direction itself never forces a switch.
    assert m.check_service_deadline(obs(60, 30, 0)) == []
    assert m.check_service_deadline(obs(61, 30, 0)) == []
