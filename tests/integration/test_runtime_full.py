"""Chunk 3 with real SUMO: full-horizon fixed-time and queue runs under full runtime enforcement.

Section J (runtime, fixed-time attribution) and Module 5 acceptance checks over 3,600 s.
"""

import json
import re

import pytest

from traffic_cps.cli import main
from traffic_cps.contracts import CompletionStatus, Decision, DecisionReason, DecisionSource
from traffic_cps.controllers.fixed import FixedTimeController
from traffic_cps.controllers.queue import QueueController
from traffic_cps.explain import explain
from traffic_cps.report import build_report
from traffic_cps.runtime import Runtime
from tests.integration.harness import CONFIG, PROJECT_ROOT, make_scenario

H = 3600
TARGET = CONFIG.signal.fixed_green_target_s


def events_of(run_dir):
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def by_type(events, kind):
    return [e for e in events if e["type"] == kind]


@pytest.fixture(scope="module")
def peak_hour(network_dir, tmp_path_factory):
    return make_scenario(network_dir, tmp_path_factory.mktemp("peak") / "scn", seed=2, profile="peak", horizon=H)


def full_run(scenario, out, controller):
    result = Runtime(CONFIG, scenario, controller, out, horizon_s=H).run()
    run_dir = out / "runs" / result.run_id
    return result, run_dir, events_of(run_dir)


@pytest.fixture(scope="module")
def fixed_run(peak_hour, tmp_path_factory):
    return full_run(peak_hour, tmp_path_factory.mktemp("fixed"), FixedTimeController(TARGET))


@pytest.fixture(scope="module")
def queue_run(peak_hour, tmp_path_factory):
    return full_run(peak_hour, tmp_path_factory.mktemp("queue"), QueueController())


@pytest.fixture(params=["fixed", "queue"])
def any_run(request):
    return request.getfixturevalue(f"{request.param}_run")


# ----------------------------------------------------------------------
# Section J runtime checks, both controllers
# ----------------------------------------------------------------------

def test_sectionJ_full_hour_3601_observations_one_per_second(any_run):
    result, _, events = any_run
    assert result.completion is CompletionStatus.COMPLETED and result.persistence_ok and result.trace_complete
    assert [e["sim_time_s"] for e in by_type(events, "observation")] == list(range(H + 1))
    assert result.counters["observations"] == H + 1 and result.counters["ticks"] == H
    assert result.final_valid_time_s == H


def test_sectionJ_720_scheduled_checks_including_skipped_time_zero(any_run):
    result, _, events = any_run
    checks = sorted(e["sim_time_s"] for e in by_type(events, "decision") + by_type(events, "constraint_hold"))
    assert checks == list(range(0, H, 5))
    assert result.counters["scheduled_checks"] == 720
    assert result.counters["eligible_choices"] + result.counters["constraint_holds"] == 720
    hold0 = by_type(events, "constraint_hold")[0]
    assert hold0["sim_time_s"] == 0 and hold0["payload"]["reason"] == "minimum_green_not_reached"


def test_sectionJ_no_command_at_or_after_horizon(any_run):
    _, _, events = any_run
    command_types = ("decision", "submission", "operating_action", "signal_change")
    assert not [e for e in events if e["sim_time_s"] >= H and e["type"] in command_types]
    assert events[-1]["type"] == "run_end" and events[-1]["payload"]["completion"] == "completed"


def test_module4A_full_hour_has_no_safety_events_or_unexpected_blocks(any_run):
    result, _, events = any_run
    for kind in ("safety_violation", "safety_override", "error"):
        assert not by_type(events, kind), kind
    assert result.counters["blocked_requests"] == 0 and result.counters["unexpected_blocks"] == 0


def test_module5_every_signal_change_has_a_recorded_origin(any_run):
    _, _, events = any_run
    ids = {e["event_id"] for e in events}
    for change in by_type(events, "signal_change"):
        origin = change["payload"]["origin"]
        if origin["kind"] == "initialization":
            assert change["sim_time_s"] == 0
        else:
            assert origin["id"] in ids, change


def test_contract_G_full_hour_report_links_match_event_ids(any_run):
    _, run_dir, events = any_run
    html = build_report(run_dir).read_text()
    ids = {e["event_id"] for e in events}
    hrefs = set(re.findall(r'href="#([^"]+)"', html))
    assert hrefs and hrefs <= ids


# ----------------------------------------------------------------------
# Fixed-time attribution (Section J)
# ----------------------------------------------------------------------

def test_sectionJ_fixed_every_switch_is_fixed_schedule(fixed_run):
    result, run_dir, events = fixed_run
    decisions = {e["event_id"]: e["payload"] for e in by_type(events, "decision")}
    assert {p["source"] for p in decisions.values()} == {"fixed_schedule"}
    yellows = [e for e in by_type(events, "signal_change") if e["payload"]["origin"]["kind"] != "initialization"
               and e["payload"]["origin"]["kind"] != "transition_completion"]
    assert yellows, "a full hour of fixed-time control must switch"
    for y in yellows:
        assert y["payload"]["origin"]["kind"] == "controller_decision"
        d = decisions[y["payload"]["origin"]["id"]]
        assert d["reason"] == "fixed_target_reached" and d["elapsed_green_s"] >= TARGET
    assert not by_type(events, "operating_action") and result.counters["operating_actions"] == 0


def test_sectionJ_fixed_decisions_follow_the_target(fixed_run):
    _, _, events = fixed_run
    for e in by_type(events, "decision"):
        p = e["payload"]
        assert p["fixed_green_target_s"] == TARGET
        assert p["retains"] == (p["elapsed_green_s"] < TARGET)


def test_sectionJ_fixed_realized_greens_are_target_then_grid_delayed(fixed_run):
    """First green: onset 0, switch at the 30 s check. Later greens start 2 s after a check, so the
    first check at or past the target comes 3 s late: realized 33 s for a 30 s target."""
    _, _, events = fixed_run
    changes = by_type(events, "signal_change")
    onsets = [c["sim_time_s"] for c in changes if c["payload"]["origin"]["kind"] in
              ("initialization", "transition_completion")]
    yellows = [c["sim_time_s"] for c in changes if c["payload"]["origin"]["kind"] == "controller_decision"]
    durations = [y - g for g, y in zip(onsets, yellows)]
    assert durations[0] == TARGET
    assert set(durations[1:]) == {TARGET + 3}
    assert all(b - a == 2 for a, b in zip(yellows, onsets[1:]))   # every yellow lasts exactly 2 s


def test_sectionJ_fixed_explanations_name_the_schedule(fixed_run):
    _, run_dir, events = fixed_run
    for e in by_type(events, "decision")[:40]:
        text = explain(run_dir, e["event_id"]).text
        assert "fixed-time schedule" in text and "Queue scores did not affect this choice." in text
        assert "queue score was higher" not in text and "tie rule" not in text


# ----------------------------------------------------------------------
# Queue rule under the full runtime
# ----------------------------------------------------------------------

class NeverSwitch:
    """Test controller that always retains, so every switch in the run is the forced service rule."""

    name = "never-switch"

    def select(self, ctx, decision_id):
        o = ctx.observation
        return Decision(decision_id, o.observation_id, o.time, o.current_green, o.current_green,
                        DecisionSource.TEST_SCRIPT, DecisionReason.SCRIPTED, dict(o.scores),
                        dict(o.counts_by_direction), False, False, o.elapsed_green_s,
                        ctx.green_service_limit_s, None, None)


def test_module5_full_hour_forced_switches_execute_same_second_with_s4(network_dir, tmp_path):
    """The queue rule never reached the service limit in probe runs (longest green 43 s), so the
    forced rule is exercised with a controller that never switches on its own."""
    scenario = make_scenario(network_dir, tmp_path / "scn", seed=0, profile="asymmetric", horizon=H)
    result, run_dir, events = full_run(scenario, tmp_path / "out", NeverSwitch())
    assert result.completion is CompletionStatus.COMPLETED
    ops = by_type(events, "operating_action")
    assert len(ops) >= 10, "a never-switching controller must hit the service limit repeatedly"
    assert result.counters["operating_actions"] == len(ops) and result.counters["switches_requested"] == 0
    changes = {(c["payload"]["origin"]["id"], c["payload"]["origin"]["kind"]): c["sim_time_s"]
               for c in by_type(events, "signal_change")}
    for op in ops:
        p = op["payload"]
        assert p["elapsed_green_s"] >= CONFIG.signal.green_service_limit_s and p["competing_measured_count"] > 0
        assert changes[(op["event_id"], "operating_action")] == op["sim_time_s"]        # yellow same second
        assert changes[(op["event_id"], "transition_completion")] == op["sim_time_s"] + 2
    yellows = [c for c in by_type(events, "signal_change") if c["payload"]["origin"]["kind"] not in
               ("initialization", "transition_completion")]
    assert {c["payload"]["origin"]["kind"] for c in yellows} == {"operating_action"}     # never the controller
    assert not by_type(events, "safety_violation") and not by_type(events, "safety_override")
    text = explain(run_dir, ops[0]["event_id"]).text
    assert "operating rule, not the controller" in text


def test_module2A_queue_full_hour_sources_are_queue_rule(queue_run):
    _, _, events = queue_run
    decisions = by_type(events, "decision")
    assert {d["payload"]["source"] for d in decisions} == {"queue_rule"}
    for d in decisions:
        p = d["payload"]
        if p["reason"] == "higher_queue_score":
            assert p["requested"] == max(p["scores"], key=p["scores"].get)
        assert p["fixed_green_target_s"] is None


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def test_contract_I_run_fixed_controller_smoke_exits_0(network_dir, tmp_path):
    scenario = make_scenario(network_dir, tmp_path / "smoke", horizon=60)
    out = tmp_path / "outputs" / "fixed"
    assert main(["run", "--config", str(PROJECT_ROOT / "configs/default.yaml"), "--controller", "fixed",
                 "--scenario", str(scenario), "--horizon", "60", "--output", str(out)]) == 0
    (run_dir,) = list((out / "runs").iterdir())
    manifest = json.loads((run_dir / "run.manifest.json").read_text())
    assert manifest["controller"] == "fixed"
    events = events_of(run_dir)
    switch = next(e for e in by_type(events, "decision") if not e["payload"]["retains"])
    assert switch["sim_time_s"] == TARGET and switch["payload"]["source"] == "fixed_schedule"
    assert (run_dir / "report.html").is_file()
