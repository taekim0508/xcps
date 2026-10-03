"""Module 4 (adapter) and Module 1 (sensor) with real SUMO: stepping, timers, transitions."""

import pytest

from traffic_cps.adapter import AdapterError
from traffic_cps.contracts import (
    BlockReason,
    Direction,
    Origin,
    OriginKind,
    SignalColor,
    SignalPhase,
    SubmissionStatus,
)
from tests.integration.harness import Harness, make_scenario

NS, WE = Direction.NS, Direction.WE


def ctrl(n):
    return Origin(OriginKind.CONTROLLER_DECISION, f"dec-{n}")


@pytest.fixture
def harness(network_dir, tmp_path):
    h = Harness.start(make_scenario(network_dir, tmp_path / "scn", profile="asymmetric"))
    yield h
    h.close()


def test_module4_initialization_ns_green_with_zero_clocks(harness):
    obs = harness.observations[0]
    assert obs.time == 0
    assert obs.phase is SignalPhase.NS_GREEN and obs.current_green is NS
    assert obs.elapsed_green_s == 0
    assert obs.colors == {NS: SignalColor.GREEN, WE: SignalColor.RED}
    assert obs.color_since_s == {NS: 0, WE: 0}
    assert obs.raw_signal == harness.adapter.signal_map.mask(SignalPhase.NS_GREEN)
    assert not harness.adapter.choice_allowed()  # minimum green incomplete at t=0


def test_module4_advances_exactly_one_second(harness):
    for expected in range(1, 11):
        assert harness.tick().time == expected


def test_module4_mask_holds_without_any_parallel_signal_program(harness):
    mask = harness.observations[0].raw_signal
    harness.run_until(100)
    assert {o.raw_signal for o in harness.observations} == {mask}
    assert harness.events == []


def test_module4_switch_timeline_10_yellow_12_green_17_eligible(harness):
    a = harness.adapter
    harness.run_until(10)
    result = a.request(WE, ctrl(1))
    assert result.status is SubmissionStatus.ACCEPTED
    # Submission is not execution: the read-back shows yellow, not the target green.
    assert result.signal_event.new_colors == {NS: SignalColor.YELLOW, WE: SignalColor.RED}
    assert a.timing().transition_target is WE and a.timing().current_green is None
    assert harness.tick().phase is SignalPhase.NS_YELLOW          # t=11
    obs12 = harness.tick()                                         # t=12
    assert obs12.phase is SignalPhase.WE_GREEN and obs12.elapsed_green_s == 0
    assert obs12.color_since_s == {NS: 12, WE: 12}
    completion = harness.events[-1]
    assert completion.signal_event.origin == Origin(OriginKind.TRANSITION_COMPLETION, "dec-1")
    harness.run_until(15)
    assert not a.choice_allowed()                                  # 3 s of green
    harness.run_until(17)
    assert a.choice_allowed()                                      # minimum green complete
    assert harness.violations == [] and harness.monitor.overrides == []


def test_module4_yellow_shorter_than_control_interval_is_captured(harness):
    harness.run_until(10)
    # Section E: the primary observation at t=10 precedes the request at t=10 (step 3 before
    # step 6), so the yellow onset is captured by the signal event and the color-onset time.
    switch = harness.adapter.request(WE, ctrl(1))
    harness.run_until(13)
    assert switch.signal_event.time == 10
    assert switch.signal_event.new_colors[NS] is SignalColor.YELLOW
    by_time = {o.time: o for o in harness.observations}
    assert by_time[10].colors[NS] is SignalColor.GREEN
    assert by_time[11].colors[NS] is SignalColor.YELLOW and by_time[11].color_since_s[NS] == 10
    assert by_time[12].colors[NS] is SignalColor.RED and by_time[12].color_since_s[NS] == 12
    # The 2 s yellow (10-11) fell entirely between the 5 s control points at 10 and 15.


def test_module4_retaining_green_does_not_reset_timer(harness):
    harness.run_until(10)
    result = harness.adapter.request(NS, ctrl(1))
    assert result.status is SubmissionStatus.ACCEPTED and result.signal_event is None
    assert harness.tick().elapsed_green_s == 11


def test_module4_minimum_green_block_schedules_nothing_later(harness):
    a = harness.adapter
    harness.run_until(10)
    a.request(WE, ctrl(1))
    harness.run_until(14)
    blocked = a.request(NS, ctrl(2))
    assert blocked.status is SubmissionStatus.BLOCKED
    assert blocked.block_reason is BlockReason.MINIMUM_GREEN_NOT_REACHED
    harness.run_until(30)
    # Still WE green: the blocked request never executed when minimum green expired.
    assert harness.observations[-1].current_green is WE
    assert len(harness.events) == 1


def test_module4_conflicting_request_cannot_retarget_transition(harness):
    a = harness.adapter
    harness.run_until(10)
    a.request(WE, ctrl(1))
    harness.tick()
    for target in (NS, WE):
        result = a.request(target, ctrl(2))
        assert result.status is SubmissionStatus.BLOCKED
        assert result.block_reason is BlockReason.TRANSITION_IN_PROGRESS
    assert a.timing().transition_target is WE
    assert harness.tick().current_green is WE


def test_module4_no_command_at_horizon(network_dir, tmp_path):
    h = Harness.start(make_scenario(network_dir, tmp_path / "scn", horizon=60), horizon=60)
    try:
        h.run_until(60)
        result = h.adapter.request(WE, ctrl(1))
        assert result.status is SubmissionStatus.BLOCKED
        assert result.block_reason is BlockReason.AT_OR_AFTER_HORIZON
        with pytest.raises(AdapterError, match="horizon"):
            h.adapter.advance_one_second()
    finally:
        h.close()


def test_module4_scenario_horizon_must_match_run_horizon(network_dir, tmp_path):
    scenario = make_scenario(network_dir, tmp_path / "scn", horizon=60)
    with pytest.raises(AdapterError, match="horizon"):
        Harness.start(scenario, horizon=120)


def test_module1_sensor_counts_match_simulator_and_share_timestamp(harness):
    obs = harness.run_until(60)
    lanes = harness.adapter._conn.lane
    for lane, count in obs.counts_by_lane.items():
        assert count == lanes.getLastStepHaltingNumber(lane)
    assert obs.counts_by_direction[WE] == obs.counts_by_lane["W_J_0"] + obs.counts_by_lane["W_J_1"]
    assert obs.counts_by_direction[WE] > 0  # W/E has been red for 60 s under asymmetric demand
