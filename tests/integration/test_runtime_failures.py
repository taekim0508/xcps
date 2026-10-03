"""Chunk 3 failure paths with real SUMO and injected faults (Module 5, Module 6 write failures, Section E).

Every failure stops the run as incomplete, keeps the records written before it, closes SUMO,
and makes `run` exit 1. An unexpected block is the one recoverable case: logged and discarded.
"""

import json
from functools import partial

import pytest

from traffic_cps import cli
from traffic_cps import events as ev
from traffic_cps.contracts import (
    CompletionStatus,
    Decision,
    DecisionReason,
    DecisionSource,
    Direction,
    SignalPhase,
)
from traffic_cps.controllers.queue import QueueController
from traffic_cps.explain import explain
from traffic_cps.report import build_report
from traffic_cps.faults import FaultyConnection, veto_write_from
from traffic_cps.runtime import Runtime
from tests.integration.harness import CONFIG, PROJECT_ROOT, make_scenario

NS, WE = Direction.NS, Direction.WE


class Scripted:
    """Test controller: request a given direction at given times, otherwise retain."""

    name = "scripted"

    def __init__(self, plan):
        self.plan = plan

    def select(self, ctx, decision_id):
        o = ctx.observation
        return Decision(decision_id, o.observation_id, o.time, self.plan.get(o.time, o.current_green),
                        o.current_green, DecisionSource.TEST_SCRIPT, DecisionReason.SCRIPTED, dict(o.scores),
                        dict(o.counts_by_direction), False, False, o.elapsed_green_s,
                        ctx.green_service_limit_s, None, None)


def events_of(run_dir):
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def by_type(events, kind):
    return [e for e in events if e["type"] == kind]


@pytest.fixture
def scenario(network_dir, tmp_path):
    return make_scenario(network_dir, tmp_path / "scn", profile="asymmetric", horizon=120)


def faulty_run(scenario, out, controller, arm):
    """Run with a FaultyConnection; `arm(faults, runtime)` sets up the injection before t=1."""
    holder = {}

    def wrap(conn):
        holder["faults"] = FaultyConnection(conn)
        arm(holder["faults"], holder["runtime"])
        return holder["faults"]

    runtime = Runtime(CONFIG, scenario, controller, out, horizon_s=120, connection_wrapper=wrap)
    holder["runtime"] = runtime
    result = runtime.run()
    return result, runtime, out / "runs" / result.run_id


def assert_stopped_at(result, run_dir, last_t):
    """Incomplete run whose log ends cleanly with run_end and no observation after `last_t`."""
    assert result.completion is CompletionStatus.INCOMPLETE and result.failure
    assert result.final_valid_time_s == last_t
    events = events_of(run_dir)
    assert events[-1]["type"] == "run_end" and events[-1]["payload"]["completion"] == "incomplete"
    assert [e["sim_time_s"] for e in by_type(events, "observation")] == list(range(last_t + 1))
    (error,) = [e for e in by_type(events, "error") if not e["payload"]["recovered"]]
    assert result.persistence_ok and result.trace_complete
    return events, error


def test_module5_sensor_failure_mid_run_stops_after_three_attempts(scenario, tmp_path):
    arm = lambda f, rt: f.at_step(20, partial(f.fail_next, "lane", "getLastStepHaltingNumber", 3))
    result, runtime, run_dir = faulty_run(scenario, tmp_path / "out", QueueController(), arm)
    events, error = assert_stopped_at(result, run_dir, 19)
    assert error["sim_time_s"] == 20 and error["payload"]["kind"] == "SensorFailure"
    assert len(error["payload"]["failures"]) == 3            # same-second retries, then explicit failure
    assert runtime.adapter._conn is None                      # SUMO closed on the failure path


def test_module5_signal_command_failure_stops_run(scenario, tmp_path):
    arm = lambda f, rt: f.at_step(10, partial(f.fail_next, "trafficlight", "setRedYellowGreenState"))
    result, _, run_dir = faulty_run(scenario, tmp_path / "out", Scripted({10: WE}), arm)
    events, error = assert_stopped_at(result, run_dir, 10)
    (sub,) = [s for s in by_type(events, "submission") if s["payload"]["origin"]["id"] == "dec-10"]
    assert sub["payload"]["status"] == "failed"
    assert not [c for c in by_type(events, "signal_change") if c["sim_time_s"] == 10]   # no change claimed
    assert result.counters["accepted_requests"] == 1          # only the retain at 5s
    assert "Submission failed" in explain(run_dir, "dec-10").text


def test_module5_unconfirmed_command_stops_run(scenario, tmp_path):
    arm = lambda f, rt: f.at_step(10, f.fail_readback_after_next_write)
    result, _, run_dir = faulty_run(scenario, tmp_path / "out", Scripted({10: WE}), arm)
    events, _ = assert_stopped_at(result, run_dir, 10)
    (sub,) = [s for s in by_type(events, "submission") if s["payload"]["origin"]["id"] == "dec-10"]
    assert sub["payload"]["status"] == "unconfirmed"
    assert sub["payload"]["signal_event_id"] is None          # submission is not execution


def test_module4A_safety_violation_mid_run_stops_and_is_logged(scenario, tmp_path):
    def arm(f, rt):  # the plant applies opposing green instead of the requested yellow
        f.at_step(10, lambda: f.substitute_next_signal_write(rt.adapter.signal_map.mask(SignalPhase.WE_GREEN)))

    result, _, run_dir = faulty_run(scenario, tmp_path / "out", Scripted({10: WE}), arm)
    events, _ = assert_stopped_at(result, run_dir, 10)
    violations = by_type(events, "safety_violation")
    assert violations and "S2" in {v["payload"]["invariant"] for v in violations}
    assert result.counters["safety_violations"] == len(violations)


def test_module5_simulator_cannot_advance_is_incomplete_not_terminal(scenario, tmp_path):
    arm = lambda f, rt: f.at_step(29, partial(f.fail_next, "connection", "simulationStep"))
    result, _, run_dir = faulty_run(scenario, tmp_path / "out", QueueController(), arm)
    events, error = assert_stopped_at(result, run_dir, 29)
    assert "simulation step failed" in error["payload"]["message"]
    assert not by_type(events, "learning_update")             # no invented terminal update


def test_module6_event_log_write_failure_stops_with_persistence_failure(scenario, tmp_path, monkeypatch):
    real_append = ev.EventWriter.append

    def append(self, event_type, sim_time_s, payload, event_id=None):
        if sim_time_s >= 25:                                  # the disk stays unwritable from 25 s on
            raise OSError(28, "No space left on device")
        return real_append(self, event_type, sim_time_s, payload, event_id)

    monkeypatch.setattr(ev.EventWriter, "append", append)
    runtime = Runtime(CONFIG, scenario, QueueController(), tmp_path / "out", horizon_s=120)
    result = runtime.run()
    assert result.completion is CompletionStatus.INCOMPLETE
    assert not result.persistence_ok and not result.trace_complete
    assert "No space left" in result.failure
    assert runtime.adapter._conn is None
    monkeypatch.undo()
    lines = (tmp_path / "out" / "runs" / result.run_id / "events.jsonl").read_text().splitlines()
    parsed = [json.loads(line) for line in lines]             # every written line is complete JSON
    assert max(e["sim_time_s"] for e in parsed) == 24
    assert parsed[-1]["type"] != "run_end"                    # the log honestly lacks its end record


def test_module5_unexpected_block_is_logged_discarded_and_not_deferred(scenario, tmp_path):
    arm = lambda f, rt: veto_write_from(rt.monitor, "dec-10")
    result, _, run_dir = faulty_run(scenario, tmp_path / "out", Scripted({10: WE}), arm)
    assert result.completion is CompletionStatus.COMPLETED and result.failure is None
    events = events_of(run_dir)
    (sub,) = [s for s in by_type(events, "submission") if s["payload"]["origin"]["id"] == "dec-10"]
    assert sub["payload"]["status"] == "blocked" and sub["payload"]["block_reason"] == "safety_veto"
    assert len(by_type(events, "safety_override")) == 1
    (diag,) = by_type(events, "error")
    assert diag["sim_time_s"] == 10 and diag["payload"]["stage"] == "unexpected_block"
    assert diag["payload"]["recovered"] is True and diag["payload"]["origin"]["id"] == "dec-10"
    # Not queued: no signal change ever comes from dec-10, and the 15 s check is a fresh retain decision.
    assert not [c for c in by_type(events, "signal_change") if c["payload"]["origin"]["id"] == "dec-10"]
    d15 = next(e for e in by_type(events, "decision") if e["sim_time_s"] == 15)
    assert d15["payload"]["requested"] == "NS" and d15["payload"]["retains"]
    c = result.counters
    assert c["blocked_requests"] == c["unexpected_blocks"] == 1 and c["switches_requested"] == 0
    assert c["accepted_requests"] == c["decisions"] - 1
    assert "blocked (safety_veto)" in explain(run_dir, "dec-10").text
    assert "Unexpected block at 10s" in build_report(run_dir).read_text()


def test_contract_I_run_failure_exits_1_with_report(scenario, tmp_path, monkeypatch, capsys):
    from traffic_cps import runtime as runtime_module

    def wrap(conn):
        f = FaultyConnection(conn)
        f.at_step(30, partial(f.fail_next, "connection", "simulationStep"))
        return f

    real = runtime_module.SumoAdapter
    monkeypatch.setattr(runtime_module, "SumoAdapter",
                        lambda *a, **kw: real(*a, **{**kw, "connection_wrapper": wrap}))
    out = tmp_path / "outputs" / "fail"
    code = cli.main(["run", "--config", str(PROJECT_ROOT / "configs/default.yaml"), "--controller", "queue",
                     "--scenario", str(scenario), "--horizon", "120", "--output", str(out)])
    assert code == 1
    assert "incomplete" in capsys.readouterr().out
    (run_dir,) = list((out / "runs").iterdir())
    assert (run_dir / "report.html").is_file()                # failures are reported, not hidden
    assert "simulation step failed" in (run_dir / "report.html").read_text()
