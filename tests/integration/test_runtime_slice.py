"""Chunk 2 vertical slice with real SUMO: runtime ordering, event log, explain, report."""

import json
import re

import pytest

from traffic_cps.cli import main
from traffic_cps.contracts import CompletionStatus, Decision, DecisionReason, DecisionSource, Direction
from traffic_cps.explain import explain
from traffic_cps.runtime import Runtime
from tests.integration.harness import CONFIG, PROJECT_ROOT, make_scenario

NS, WE = Direction.NS, Direction.WE


class Scripted:
    """Test controller: request a fixed direction at given times, otherwise retain."""

    name = "scripted"

    def __init__(self, plan):
        self.plan = plan

    def select(self, ctx, decision_id):
        o = ctx.observation
        requested = self.plan.get(o.time, o.current_green)
        return Decision(decision_id, o.observation_id, o.time, requested, o.current_green,
                        DecisionSource.TEST_SCRIPT, DecisionReason.SCRIPTED, dict(o.scores),
                        dict(o.counts_by_direction), False, False, o.elapsed_green_s,
                        ctx.green_service_limit_s, None, None)


def events_of(run_dir):
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def by_type(events, kind):
    return [e for e in events if e["type"] == kind]


def run(network_dir, tmp_path, controller, profile="balanced", horizon=60):
    scenario = make_scenario(network_dir, tmp_path / "scn", profile=profile, horizon=horizon)
    return Runtime(CONFIG, scenario, controller, tmp_path / "out", horizon_s=horizon).run()


def test_module5_cli_smoke_run_end_to_end(network_dir, tmp_path):
    scenario = make_scenario(network_dir, tmp_path / "smoke", horizon=60)
    out = tmp_path / "outputs" / "smoke"
    assert main(["run", "--config", str(PROJECT_ROOT / "configs/default.yaml"), "--controller", "queue",
                 "--scenario", str(scenario), "--horizon", "60", "--output", str(out)]) == 0
    (run_dir,) = list((out / "runs").iterdir())
    for name in ["config.resolved.json", "scenario.manifest.json", "run.manifest.json",
                 "policy_start.json", "events.jsonl", "report.html"]:
        assert (run_dir / name).is_file(), name
    assert json.loads((run_dir / "policy_start.json").read_text()) is None   # baseline: no Q-table

    events = events_of(run_dir)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["type"] == "run_start" and events[-1]["type"] == "run_end"
    assert events[-1]["payload"]["completion"] == "completed"
    assert [e["sim_time_s"] for e in by_type(events, "observation")] == list(range(61))
    checks = by_type(events, "decision") + by_type(events, "constraint_hold")
    assert sorted(e["sim_time_s"] for e in checks) == list(range(0, 60, 5))      # 12 checks, none at H
    assert not [e for e in events if e["sim_time_s"] == 60 and e["type"] in
                ("decision", "submission", "operating_action")]
    assert not by_type(events, "safety_violation") and not by_type(events, "safety_override")
    hold0 = by_type(events, "constraint_hold")[0]
    assert hold0["sim_time_s"] == 0 and hold0["payload"]["reason"] == "minimum_green_not_reached"

    decision = by_type(events, "decision")[0]
    assert main(["explain", "--run", str(run_dir), "--decision", decision["event_id"]]) == 0
    html = (run_dir / "report.html").read_text()
    ids = {e["event_id"] for e in events}
    assert set(re.findall(r'href="#([^"]+)"', html)) <= ids


def test_section_J_timeline_10_switch_12_green_20_next_choice(network_dir, tmp_path):
    result = run(network_dir, tmp_path, Scripted({10: WE}), horizon=30)
    assert result.completion is CompletionStatus.COMPLETED
    events = events_of(tmp_path / "out" / "runs" / result.run_id)
    changes = [(e["sim_time_s"], e["payload"]["origin"]["kind"], e["payload"]["origin"]["id"])
               for e in by_type(events, "signal_change")]
    assert changes == [(0, "initialization", "init"), (10, "controller_decision", "dec-10"),
                       (12, "transition_completion", "dec-10")]
    holds = {e["sim_time_s"]: e["payload"]["reason"] for e in by_type(events, "constraint_hold")}
    assert holds == {0: "minimum_green_not_reached", 15: "minimum_green_not_reached"}
    decisions = [e["sim_time_s"] for e in by_type(events, "decision")]
    assert decisions == [5, 10, 20, 25]
    text = explain(tmp_path / "out" / "runs" / result.run_id, "dec-10").text
    assert "N/S turned yellow at 10s, and W/E became green at 12s." in text


def test_module5_forced_switch_is_operating_action_not_decision(network_dir, tmp_path):
    result = run(network_dir, tmp_path, Scripted({}), profile="asymmetric", horizon=120)
    run_dir = tmp_path / "out" / "runs" / result.run_id
    events = events_of(run_dir)
    (op,) = by_type(events, "operating_action")
    assert op["sim_time_s"] == 60
    assert op["payload"]["reason"] == "service_limit_competing_demand"
    assert op["payload"]["competing_measured_count"] > 0
    changes = {e["sim_time_s"]: e["payload"]["origin"] for e in by_type(events, "signal_change")}
    assert changes[60] == {"kind": "operating_action", "id": op["event_id"]}
    assert changes[62]["kind"] == "transition_completion" and changes[62]["id"] == op["event_id"]
    assert not [d for d in by_type(events, "decision") if d["sim_time_s"] == 60 and not d["payload"]["retains"]]
    text = explain(run_dir, op["event_id"]).text
    assert "operating rule" in text and "service limit" in text
    assert not by_type(events, "safety_violation")
    assert result.counters["operating_actions"] == 1


def test_module5_counters_are_distinct(network_dir, tmp_path):
    result = run(network_dir, tmp_path, Scripted({10: WE}), horizon=60)
    c = result.counters
    assert c["ticks"] == 60 and c["observations"] == 61 and c["scheduled_checks"] == 12
    assert c["eligible_choices"] == c["decisions"] == 10       # 12 checks minus holds at 0 and 15
    assert c["accepted_requests"] == 10 and c["switches_requested"] == 1
    assert c["safety_overrides"] == 0 and c["safety_violations"] == 0


def test_module2A_queue_run_explanations_match_recorded_branch(network_dir, tmp_path):
    from traffic_cps.controllers.queue import QueueController

    result = run(network_dir, tmp_path, QueueController(), profile="asymmetric", horizon=120)
    run_dir = tmp_path / "out" / "runs" / result.run_id
    for d in by_type(events_of(run_dir), "decision"):
        p = d["payload"]
        text = explain(run_dir, d["event_id"]).text
        if p["reason"] == "equal_queue_scores":
            assert "tie rule" in text
        elif p["reason"] == "higher_queue_score":
            assert "queue score was higher" in text
        winner = max(p["scores"], key=p["scores"].get)
        if p["reason"] == "higher_queue_score":
            assert p["requested"] == winner


def test_contract_B_saturation_check_measures_peak_window(network_dir, tmp_path):
    from traffic_cps.experiments import ExperimentError, saturation_check

    record = saturation_check(CONFIG, network_dir, seed=2, output_dir=tmp_path / "sat", horizon_s=300,
                              record_in_network=False)
    assert record["peak_window_s"] == [100, 200]
    assert record["peak_probability_per_direction"] == 0.45
    assert 0 < record["free_choice_checks_in_window"] <= 20      # at most one check per 5 s in the window
    assert record["passed"] == (record["fraction"] <= record["max_fraction"])
    assert (tmp_path / "sat" / "saturation_check.json").is_file()
    with pytest.raises(ExperimentError, match="final"):
        saturation_check(CONFIG, network_dir, seed=200, output_dir=tmp_path / "sat2", horizon_s=300,
                         record_in_network=False)
