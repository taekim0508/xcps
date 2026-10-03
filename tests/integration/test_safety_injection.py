"""Module 4A and Section J (safety/faults) with real SUMO and injected faults."""

import pytest

from traffic_cps.adapter import AdapterError
from traffic_cps.contracts import Direction, Origin, OriginKind, SignalPhase, SubmissionStatus
from traffic_cps.faults import FaultyConnection, disable_adapter_timing_guard
from traffic_cps.sensor import SensorFailure
from tests.integration.harness import Harness, make_scenario

NS, WE = Direction.NS, Direction.WE


def ctrl(n):
    return Origin(OriginKind.CONTROLLER_DECISION, f"dec-{n}")


@pytest.fixture
def scenario(network_dir, tmp_path):
    return make_scenario(network_dir, tmp_path / "scn", profile="asymmetric")


def start_faulty(scenario):
    holder = {}

    def wrap(conn):
        holder["faults"] = FaultyConnection(conn)
        return holder["faults"]

    h = Harness.start(scenario, connection_wrapper=wrap)
    return h, holder["faults"]


def test_module4A_normal_run_has_zero_overrides_and_violations(scenario):
    h = Harness.start(scenario)
    try:
        h.run_until(10)
        h.adapter.request(WE, ctrl(1))
        h.run_until(40)
        h.adapter.request(NS, ctrl(2))
        h.run_until(120)
        assert h.violations == [] and h.monitor.violations == [] and h.monitor.overrides == []
    finally:
        h.close()


def test_module4A_direct_green_to_green_caught_logged_and_stops(scenario):
    h, faults = start_faulty(scenario)
    try:
        h.run_until(10)
        green_we = h.adapter.signal_map.mask(SignalPhase.WE_GREEN)
        faults.substitute_next_signal_write(green_we)   # plant applies opposing green instead of yellow
        result = h.adapter.request(WE, ctrl(1))
        assert result.status is SubmissionStatus.FAILED
        assert {v.invariant for v in result.violations} >= {"S2"}
        v = result.violations[0]
        assert v.raw_signal == green_we and v.previous_raw_signal is not None   # raw states logged
        assert h.adapter.failed is not None
        with pytest.raises(AdapterError):
            h.adapter.advance_one_second()                # never continue after a violation
    finally:
        h.close()


def test_module4A_every_second_requests_blocked_S3_pre_command(scenario):
    h = Harness.start(scenario)
    try:
        disable_adapter_timing_guard(h.adapter)           # simulate an adapter bug that skips minimum green
        h.run_until(10)
        h.adapter.request(WE, ctrl(1))
        h.run_until(12)                                   # WE green at 12
        blocked = []
        for n in range(2, 6):                             # t = 12..15: a faulty controller asks every second
            result = h.adapter.request(NS, ctrl(n))
            blocked.append(result)
            h.tick()
        assert all(r.status is SubmissionStatus.BLOCKED for r in blocked)
        assert all(r.override is not None and r.override.invariant == "S3" for r in blocked)
        assert [o.origin.origin_id for o in h.monitor.overrides] == ["dec-2", "dec-3", "dec-4", "dec-5"]
        assert h.violations == [] and h.monitor.violations == []  # nothing unsafe ever executed
    finally:
        h.close()


def test_module4A_late_forced_switch_flagged_S4(scenario):
    h = Harness.start(scenario)
    try:
        obs = h.run_until(60)
        assert obs.counts_by_direction[WE] > 0
        assert h.monitor.check_service_deadline(obs) == []   # due at 60; deadline 61
        obs = h.tick()                                       # enforcement "forgotten"
        assert [v.invariant for v in h.monitor.check_service_deadline(obs)] == ["S4"]
    finally:
        h.close()


def test_module4A_timely_forced_switch_passes_S4(scenario):
    h = Harness.start(scenario)
    try:
        obs = h.run_until(60)
        h.adapter.request(WE, Origin(OriginKind.OPERATING_ACTION, "op-1"))
        assert h.monitor.check_service_deadline(obs) == []
        assert h.monitor.check_service_deadline(h.tick()) == []
        assert h.monitor.violations == []
    finally:
        h.close()


def test_module4A_monitor_reads_raw_state_not_adapter_timers(scenario):
    """Corrupt the adapter's own green timer: only the monitor's raw history can stop the switch."""
    h = Harness.start(scenario)
    try:
        h.run_until(10)
        h.adapter.request(WE, ctrl(1))
        h.run_until(14)                  # yellow 10-11, WE green since 12: 2 s of green
        h.adapter._green_onset = -100    # adapter now wrongly believes WE has been green for 114 s
        assert h.adapter.choice_allowed()  # the adapter's guard is fooled ...
        result = h.adapter.request(NS, ctrl(2))
        assert result.status is SubmissionStatus.BLOCKED          # ... the monitor is not
        assert result.override.invariant == "S3"
        assert h.monitor.violations == []
    finally:
        h.close()


def test_section_J_missing_read_recovers_with_same_time_retries(scenario):
    h, faults = start_faulty(scenario)
    try:
        h.run_until(5)
        faults.fail_next("lane", "getLastStepHaltingNumber", times=2)
        obs = h.tick()
        assert obs.time == 6
        assert obs.measurement.attempts == 3
        assert [f.time for f in obs.measurement.failures] == [6, 6]
        assert h.adapter.time == 6  # retries never advanced the simulation
    finally:
        h.close()


def test_section_J_persistent_read_failure_stops(scenario):
    h, faults = start_faulty(scenario)
    try:
        h.run_until(5)
        faults.fail_next("lane", "getLastStepHaltingNumber", times=3)
        with pytest.raises(SensorFailure) as info:
            h.tick()
        assert len(info.value.failures) == 3
        assert h.adapter.time == 6
    finally:
        h.close()


def test_module4_command_error_no_resubmission_and_marked_failed(scenario):
    h, faults = start_faulty(scenario)
    try:
        h.run_until(10)
        faults.fail_next("trafficlight", "setRedYellowGreenState", times=1)
        writes_before = faults.calls("trafficlight", "setRedYellowGreenState")
        result = h.adapter.request(WE, ctrl(1))
        assert result.status is SubmissionStatus.FAILED and result.error
        again = h.adapter.request(WE, ctrl(2))
        assert again.status is SubmissionStatus.FAILED
        assert faults.calls("trafficlight", "setRedYellowGreenState") == writes_before + 1  # no retry
        assert h.adapter.failed is not None
        assert h.adapter.last_verified_mask == h.adapter.signal_map.mask(SignalPhase.NS_GREEN)
    finally:
        h.close()


def test_module4_unexpected_signal_change_stops(scenario):
    h, faults = start_faulty(scenario)
    try:
        h.run_until(10)
        faults.inner.trafficlight.setRedYellowGreenState("J0", h.adapter.signal_map.mask(SignalPhase.WE_GREEN))  # outside writer
        with pytest.raises(AdapterError, match="unexpected signal"):
            h.tick()
        assert any(v.invariant == "S2" for v in h.violations + h.monitor.violations)
    finally:
        h.close()


def test_module4_collision_or_teleport_stops_run(scenario):
    h, faults = start_faulty(scenario)
    try:
        h.run_until(5)
        faults.return_next("simulation", "getCollidingVehiclesIDList", ("veh0",))
        with pytest.raises(AdapterError, match="collision"):
            h.tick()
    finally:
        h.close()
