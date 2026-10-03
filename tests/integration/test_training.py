"""Chunk 4 with real SUMO: feedback windows, update timing, cutoff, failure discard, training, frozen evaluation.

Module 2B, Module 5 learning checks, and Section J (runtime rewards, learning).
"""

import json
import re
from functools import partial

import pytest

from traffic_cps.cli import main
from traffic_cps.contracts import (
    CompletionStatus,
    Decision,
    DecisionReason,
    DecisionSource,
    Direction,
    Mode,
)
from traffic_cps.controllers.q_learning import QLearningController
from traffic_cps.explain import explain
from traffic_cps.faults import FaultyConnection, veto_write_from
from traffic_cps.learning import Learner, QTable, state_from_key
from traffic_cps.report import build_report
from traffic_cps.runtime import Runtime, train
from tests.integration.harness import CONFIG, PROJECT_ROOT, make_scenario

NS, WE = Direction.NS, Direction.WE


class ScriptedQ:
    """Training test controller: deterministic requests, but with a real Q-state so feedback is tracked."""

    name = "scripted-q"

    def __init__(self, learner, plan):
        self.learner, self.plan = learner, plan

    def select(self, ctx, decision_id):
        o = ctx.observation
        return Decision(decision_id, o.observation_id, o.time, self.plan.get(o.time, o.current_green),
                        o.current_green, DecisionSource.TEST_SCRIPT, DecisionReason.SCRIPTED, dict(o.scores),
                        dict(o.counts_by_direction), False, False, o.elapsed_green_s, ctx.green_service_limit_s,
                        None, self.learner.table.revision, q_state=self.learner.encode(o))


def events_of(run_dir):
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def by_type(events, kind):
    return [e for e in events if e["type"] == kind]


def scripted_run(network_dir, tmp_path, plan, horizon, profile="balanced", wrap=None):
    scenario = make_scenario(network_dir, tmp_path / "scn", profile=profile, horizon=horizon)
    learner = Learner(CONFIG)
    holder = {}

    def connection_wrapper(conn):
        return wrap(conn, holder["runtime"]) if wrap else conn

    runtime = Runtime(CONFIG, scenario, ScriptedQ(learner, plan), tmp_path / "out", horizon_s=horizon,
                      mode=Mode.TRAIN, learner=learner, connection_wrapper=connection_wrapper)
    holder["runtime"] = runtime
    result = runtime.run()
    run_dir = tmp_path / "out" / "runs" / result.run_id
    return result, learner, run_dir, events_of(run_dir)


def updates_by_decision(events):
    return {u["payload"]["decision_id"]: u for u in by_type(events, "learning_update")}


# ----------------------------------------------------------------------
# Update timing: 10 -> 12 -> 20, rewards once per second, update before selection
# ----------------------------------------------------------------------

def test_module5_switch_at_10_updates_at_20_not_12_15_17(network_dir, tmp_path):
    result, learner, run_dir, events = scripted_run(network_dir, tmp_path, {10: WE}, horizon=30)
    assert result.completion is CompletionStatus.COMPLETED
    ups = updates_by_decision(events)
    assert [(u["sim_time_s"], d) for d, u in ups.items()] == [(10, "dec-5"), (20, "dec-10"), (25, "dec-20"),
                                                             (30, "dec-25")]
    u10 = ups["dec-10"]["payload"]
    assert u10["start_time_s"] == 10 and u10["end_time_s"] == 20 and u10["duration_s"] == 10
    assert u10["sample_ids"] == [f"obs-{t}" for t in range(11, 21)]          # yellow and min green included
    assert u10["action"] == "WE"
    assert not [u for u in by_type(events, "learning_update") if u["sim_time_s"] in (12, 15, 17)]


def test_sectionJ_rewards_counted_once_per_second_from_observed_scores(network_dir, tmp_path):
    _, _, _, events = scripted_run(network_dir, tmp_path, {10: WE}, horizon=30)
    scores = {e["event_id"]: e["payload"]["scores"] for e in by_type(events, "observation")}
    used = []
    for u in by_type(events, "learning_update"):
        p = u["payload"]
        assert p["rewards"] == [-(scores[i]["NS"] + scores[i]["WE"]) for i in p["sample_ids"]]
        used += p["sample_ids"]
    assert used == [f"obs-{t}" for t in range(6, 31)]       # every second after the first choice, exactly once


def test_module5_update_precedes_next_selection_and_decision_cites_new_revision(network_dir, tmp_path):
    _, _, _, events = scripted_run(network_dir, tmp_path, {10: WE}, horizon=30)
    seq = {e["event_id"]: e["seq"] for e in events}
    ups = updates_by_decision(events)
    decisions = {d["event_id"]: d["payload"] for d in by_type(events, "decision")}
    assert seq[ups["dec-10"]["event_id"]] < seq["dec-20"]
    assert decisions["dec-20"]["policy_revision"] == ups["dec-10"]["payload"]["policy_revision"] == 2
    assert [u["payload"]["policy_revision"] for u in by_type(events, "learning_update")] == [1, 2, 3, 4]


def test_section_E_horizon_update_when_fresh_choice_allowed_without_command(network_dir, tmp_path):
    result, learner, _, events = scripted_run(network_dir, tmp_path, {10: WE}, horizon=30)
    last = by_type(events, "learning_update")[-1]
    assert last["sim_time_s"] == 30 and last["payload"]["decision_id"] == "dec-25"
    assert last["payload"]["next_state"] is not None and len(last["payload"]["bootstrap_values"]) == 2
    assert not [e for e in events if e["sim_time_s"] == 30 and e["type"] in ("decision", "submission")]
    assert not by_type(events, "feedback_discarded")
    assert learner.schedule.accepted_choices == result.counters["accepted_requests"] == 4    # 5, 10, 20, 25


# ----------------------------------------------------------------------
# Forced switch, cutoff discard, blocked request, failure discard
# ----------------------------------------------------------------------

def test_module5_forced_switch_extends_pending_feedback_without_new_choice(network_dir, tmp_path):
    result, learner, _, events = scripted_run(network_dir, tmp_path, {}, horizon=120, profile="asymmetric")
    (op,) = by_type(events, "operating_action")
    assert op["sim_time_s"] == 60
    ups = updates_by_decision(events)
    u = ups["dec-55"]["payload"]
    assert ups["dec-55"]["sim_time_s"] == 70 and u["duration_s"] == 15       # through yellow and min green
    assert u["sample_ids"] == [f"obs-{t}" for t in range(56, 71)]
    assert not [x for x in by_type(events, "learning_update") if x["sim_time_s"] in (60, 62, 65)]
    assert not [d for d in by_type(events, "decision") if d["sim_time_s"] in (60, 65)]
    assert learner.schedule.accepted_choices == result.counters["accepted_requests"]   # forced: no decay


def test_module5_cutoff_during_minimum_green_discards_but_run_completes(network_dir, tmp_path):
    result, learner, run_dir, events = scripted_run(network_dir, tmp_path, {55: WE}, horizon=60)
    assert result.completion is CompletionStatus.COMPLETED
    (disc,) = by_type(events, "feedback_discarded")
    p = disc["payload"]
    assert disc["sim_time_s"] == 60 and p["decision_id"] == "dec-55" and p["reason"] == "minimum_green_not_reached"
    assert p["sample_ids"] == [f"obs-{t}" for t in range(56, 61)]          # diagnostic samples kept
    assert "dec-55" not in updates_by_decision(events)
    assert len(by_type(events, "learning_update")) == learner.table.revision == 10   # earlier updates kept
    assert "discarded (minimum_green_not_reached)" in explain(run_dir, disc["event_id"]).text


def test_section_E_cutoff_with_due_forced_switch_is_not_a_bootstrap_boundary(network_dir, tmp_path):
    _, _, _, events = scripted_run(network_dir, tmp_path, {}, horizon=60, profile="asymmetric")
    final = by_type(events, "observation")[-1]["payload"]
    assert final["elapsed_green_s"] == 60 and final["counts_by_direction"]["WE"] > 0   # precondition holds
    (disc,) = by_type(events, "feedback_discarded")
    assert disc["payload"]["reason"] == "service_limit_due" and disc["payload"]["decision_id"] == "dec-55"
    assert not by_type(events, "operating_action")                                   # no command at H


def test_module5_blocked_request_opens_no_feedback_and_no_epsilon_decay(network_dir, tmp_path):
    def wrap(conn, runtime):
        veto_write_from(runtime.monitor, "dec-10")
        return conn

    result, learner, _, events = scripted_run(network_dir, tmp_path, {10: WE}, horizon=30, wrap=wrap)
    ups = updates_by_decision(events)
    assert ups["dec-5"]["sim_time_s"] == 10                       # closed before the blocked selection
    assert "dec-10" not in ups
    assert not [u for u in by_type(events, "learning_update") if u["sim_time_s"] == 15]   # nothing pending at 15
    assert not by_type(events, "feedback_discarded")
    assert result.counters["unexpected_blocks"] == 1
    assert learner.schedule.accepted_choices == result.counters["accepted_requests"] == 4  # 5, 15, 20, 25


def test_module5_failure_discards_unfinished_feedback_and_keeps_completed_updates(network_dir, tmp_path):
    def wrap(conn, runtime):
        f = FaultyConnection(conn)
        f.at_step(23, partial(f.fail_next, "lane", "getLastStepHaltingNumber", 3))
        return f

    result, learner, _, events = scripted_run(network_dir, tmp_path, {}, horizon=60, wrap=wrap)
    assert result.completion is CompletionStatus.INCOMPLETE
    assert [u["payload"]["decision_id"] for u in by_type(events, "learning_update")] == ["dec-5", "dec-10", "dec-15"]
    (disc,) = by_type(events, "feedback_discarded")
    assert disc["payload"]["decision_id"] == "dec-20" and disc["payload"]["reason"] == "run_failed"
    assert learner.table.revision == 3


# ----------------------------------------------------------------------
# Training episodes and frozen evaluation
# ----------------------------------------------------------------------

def replay(start: dict, updates: list) -> QTable:
    """Rebuild a table from policy_start plus ordered updates, validating each old value (Section F)."""
    table = QTable.from_json(start["table"])
    for u in updates:
        p = u["payload"]
        state, action = state_from_key(p["state"]), Direction(p["action"])
        assert table.values(state)[action] == p["old_q"], u["event_id"]
        assert table.update_counts(state)[action] == p["update_count_before"], u["event_id"]
        table._set(state, action, p["new_q"], p["update_count_after"])
        table.revision = p["policy_revision"]
    return table


def same_policy(a: QTable, b: QTable) -> bool:
    states = set(a.states()) | set(b.states())   # zero-initialized encountered states may differ
    return a.revision == b.revision and all(
        a.values(s) == b.values(s) and a.update_counts(s) == b.update_counts(s) for s in states)


@pytest.fixture(scope="module")
def trained(network_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("training") / "training_001"
    code = main(["train", "--config", str(PROJECT_ROOT / "configs/default.yaml"), "--episodes", "2",
                 "--horizon", "300", "--output", str(out)])
    return code, out


def test_contract_I_train_two_short_episodes_exits_0_and_learns(trained):
    code, out = trained
    assert code == 0
    runs = sorted((out / "runs").iterdir())
    assert [r.name for r in runs] == ["train-ep001-seed000", "train-ep002-seed001"]
    for run_dir in runs:
        events = events_of(run_dir)
        assert events[-1]["payload"]["completion"] == "completed"
        assert by_type(events, "learning_update")
        assert {d["payload"]["source"] for d in by_type(events, "decision")} <= {"q_greedy", "q_exploration"}
        assert (run_dir / "report.html").is_file()


def test_section_F_policy_start_plus_updates_reconstructs_next_episode_start(trained):
    _, out = trained
    ep1, ep2 = sorted((out / "runs").iterdir())
    start1 = json.loads((ep1 / "policy_start.json").read_text())
    start2 = json.loads((ep2 / "policy_start.json").read_text())
    assert start1["table"]["revision"] == 0 and start1["exploration"]["epsilon"] == 1.0
    rebuilt = replay(start1, by_type(events_of(ep1), "learning_update"))
    assert same_policy(rebuilt, QTable.from_json(start2["table"]))


def test_module2B_epsilon_progress_carries_across_episodes(trained):
    _, out = trained
    ep1, ep2 = sorted((out / "runs").iterdir())
    accepted1 = events_of(ep1)[-1]["payload"]["counters"]["accepted_requests"]
    start2 = json.loads((ep2 / "policy_start.json").read_text())["exploration"]
    assert start2["accepted_choices"] == accepted1
    assert start2["epsilon"] == pytest.approx(0.9995 ** accepted1, rel=1e-12)


def test_module5_episode_reset_never_carries_pending_feedback(trained):
    _, out = trained
    _, ep2 = sorted((out / "runs").iterdir())
    events = events_of(ep2)
    decisions = {d["event_id"]: d["sim_time_s"] for d in by_type(events, "decision")}
    assert by_type(events, "learning_update")
    for u in by_type(events, "learning_update"):
        assert decisions[u["payload"]["decision_id"]] == u["payload"]["start_time_s"] < u["sim_time_s"]


def test_module7_training_events_are_explainable_and_report_links_resolve(trained):
    _, out = trained
    run_dir = sorted((out / "runs").iterdir())[0]
    events = events_of(run_dir)
    sources = [d["payload"]["source"] for d in by_type(events, "decision")]
    assert "q_exploration" in sources and by_type(events, "learning_update")   # epsilon starts at 1.0
    for e in by_type(events, "decision") + by_type(events, "learning_update"):
        text = explain(run_dir, e["event_id"]).text
        if e["type"] == "decision" and e["payload"]["source"] == "q_exploration":
            assert "explored" in text and "did not determine this choice" in text
    ids = {e["event_id"] for e in events}
    assert set(re.findall(r'href="#([^"]+)"', build_report(run_dir).read_text())) <= ids


def test_contract_I_train_refuses_existing_output(trained, capsys):
    _, out = trained
    assert main(["train", "--config", str(PROJECT_ROOT / "configs/default.yaml"), "--episodes", "1",
                 "--horizon", "60", "--output", str(out)]) == 2


def test_module2B_frozen_evaluation_leaves_table_rng_and_progress_unchanged(network_dir, tmp_path):
    learner, results = train(CONFIG, 1, tmp_path / "training", horizon_s=300)
    assert results[0].completion is CompletionStatus.COMPLETED and learner.table.revision > 0
    table_before, rng_before = learner.table.to_json(), learner.rng.getstate()
    schedule_before = learner.schedule.to_json()

    scenario = make_scenario(network_dir, tmp_path / "dev", seed=100, profile="balanced", horizon=300)
    result = Runtime(CONFIG, scenario, QLearningController(learner), tmp_path / "eval", horizon_s=300,
                     mode=Mode.EVALUATE, learner=learner).run()
    assert result.completion is CompletionStatus.COMPLETED
    assert learner.table.to_json() == table_before                     # no updates, no inserted entries
    assert learner.rng.getstate() == rng_before and learner.schedule.to_json() == schedule_before

    run_dir = tmp_path / "eval" / "runs" / result.run_id
    events = events_of(run_dir)
    assert not by_type(events, "learning_update") and not by_type(events, "feedback_discarded")
    assert json.loads((run_dir / "policy_start.json").read_text())["table"] == table_before
    decisions = by_type(events, "decision")
    assert decisions and {d["payload"]["source"] for d in decisions} <= {"q_greedy", "queue_fallback"}
    for d in decisions:
        p = d["payload"]
        assert p["epsilon"] is None and p["exploration_draw"] is None
        assert (p["source"] == "queue_fallback") == (p["fallback_reason"] is not None)
        explain(run_dir, d["event_id"])                                # every evaluation decision explains
