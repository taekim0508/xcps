"""Chunk 6 with real SUMO: explanation fidelity on all three controllers, counterfactual verification,
policy grid against the checkpoint, report links, and summary metrics.

Module 7 acceptance checks and Section G.
"""

import json
import re

import pytest

from traffic_cps import checkpoints as ck
from traffic_cps.cli import main
from traffic_cps.contracts import Direction, QState
from traffic_cps.controllers.fixed import FixedTimeController
from traffic_cps.controllers.q_learning import QLearningController, greedy_choice
from traffic_cps.controllers.queue import QueueController
from traffic_cps.contracts import Mode
from traffic_cps.explain import explain_event, load_trace, run_policy_views
from traffic_cps.learning import QTable
from traffic_cps.report import build_report, summarize
from traffic_cps.runtime import Runtime, train
from tests.integration.harness import CONFIG, PROJECT_ROOT, make_scenario

H = 600
TOL = 1e-9


@pytest.fixture(scope="module")
def runs(network_dir, tmp_path_factory):
    root = tmp_path_factory.mktemp("explain")
    scenario = make_scenario(network_dir, root / "scn", seed=100, profile="balanced", horizon=H)
    out = {}
    for controller in (FixedTimeController(30), QueueController()):
        r = Runtime(CONFIG, scenario, controller, root / controller.name, horizon_s=H).run()
        out[controller.name] = root / controller.name / "runs" / r.run_id
    trained = train(CONFIG, 2, root / "training", horizon_s=H)
    assert trained.ok
    out["training"] = root / "training" / "runs" / trained.episodes[-1].run_id
    checkpoint = root / "training" / "checkpoints" / ck.LATEST
    data = ck.CheckpointStore(checkpoint.parent).load(
        checkpoint, ck.compatibility(CONFIG, json.loads((scenario / "scenario.manifest.json").read_text())["network"],
                                     H), "evaluate")
    learner = ck.learner_from_checkpoint(CONFIG, data)
    r = Runtime(CONFIG, scenario, QLearningController(learner), root / "frozen", horizon_s=H, mode=Mode.EVALUATE,
                learner=learner, provenance={"checkpoint_id": data["checkpoint_id"]}).run()
    out["frozen"] = root / "frozen" / "runs" / r.run_id
    out["checkpoint"] = data
    return out


def decisions(trace):
    return [e for e in trace.events if e["type"] == "decision"]


@pytest.mark.parametrize("which", ["fixed", "queue", "training", "frozen"])
def test_module7_every_decision_reason_matches_its_recorded_branch(runs, which):
    trace = load_trace(runs[which])
    ds = decisions(trace)
    assert ds
    for d in ds:
        x = explain_event(trace, d["event_id"])
        assert x.facts["fidelity"] == "verified", (d["event_id"], x.limitations)
        assert x.facts["source"] == d["payload"]["source"] and x.facts["reason"] == d["payload"]["reason"]
        assert x.contrast                                                        # every decision has a contrast line
        assert not any("fidelity check failed" in lim for lim in x.limitations)


def test_module7_exploration_explained_as_exploration_even_when_it_matches_greedy(runs):
    trace = load_trace(runs["training"])
    explored = [d for d in decisions(trace) if d["payload"]["source"] == "q_exploration"]
    assert explored
    matched = 0
    for d in explored:
        p = d["payload"]
        x = explain_event(trace, d["event_id"])
        assert "explored" in x.text and "did not determine this choice" in x.text
        assert "not applicable" in x.contrast.lower()
        greedy = greedy_choice({Direction(k): v for k, v in p["q_values"].items()},
                               Direction(p["current_green"]), TOL)[0].value
        matched += greedy == p["requested"]
    assert matched > 0                                                           # the case the rule is about occurred


def brute_force_nearest(table: QTable, state: QState, requested: Direction):
    """Independent check: every fully covered differing candidate, minimum distance."""
    best = None
    for ns in range(5):
        for we in range(5):
            cand = QState(ns, we, state.green, state.elapsed_bin)
            competing = we if state.green is Direction.NS else ns
            if (ns, we) == (state.ns_bin, state.we_bin) or not table.contains(cand) \
                    or min(table.update_counts(cand).values()) == 0 or (state.elapsed_bin == 3 and competing > 0):
                continue
            if greedy_choice(table.values(cand), state.green, TOL)[0] is not requested:
                d = abs(ns - state.ns_bin) + abs(we - state.we_bin)
                best = d if best is None else min(best, d)
    return best


@pytest.mark.parametrize("which", ["training", "frozen"])
def test_sectionG_counterfactuals_recompute_to_the_stated_action_and_are_minimal(runs, which):
    trace = load_trace(runs[which])
    checked = found = 0
    for d in decisions(trace):
        p = d["payload"]
        if p["source"] != "q_greedy" or min(p["q_update_counts"].values()) == 0:
            continue
        x = explain_event(trace, d["event_id"])
        table = trace.policy.table_at(p["policy_revision"])
        state = QState(p["q_state"][0], p["q_state"][1], Direction(p["q_state"][2]), p["q_state"][3])
        nearest = brute_force_nearest(table, state, Direction(p["requested"]))
        cf = x.facts["counterfactual"]
        if nearest is None:
            assert cf == {"status": "none"} and "No supported counterfactual" in x.contrast
        else:
            cand = QState(*cf["state"][:2], Direction(cf["state"][2]), cf["state"][3])
            assert greedy_choice(table.values(cand), state.green, TOL)[0].value == cf["action"] != p["requested"]
            assert cf["distance"] == nearest                                     # no closer differing state
            found += 1
        checked += 1
    assert checked > 0
    if which == "training":   # deterministic for these seeds: training reaches a flip, the small frozen table
        assert found > 0      # (every covered state with one green prefers the same action) does not


def test_module7_policy_grid_matches_checkpoint_cell_by_cell(runs):
    trace = load_trace(runs["frozen"])
    (view,) = run_policy_views(trace)
    data = runs["checkpoint"]
    table = QTable.from_json({"revision": data["progress"]["policy_revision"], "entries": data["q_table"]})
    assert data["checkpoint_id"] in view["label"]
    covered = 0
    for g in view["grids"]:
        green = Direction(g["green"])
        for ns, row in enumerate(g["cells"]):
            for we, c in enumerate(row):
                s = QState(ns, we, green, g["elapsed_bin"])
                if c["status"] == "covered":
                    covered += 1
                    assert c["greedy"] == greedy_choice(table.values(s), green, TOL)[0].value
                    assert c["counts"] == {a.value: n for a, n in table.update_counts(s).items()}
                elif c["status"] == "fallback_unseen":
                    assert not table.contains(s)
                elif c["status"] == "fallback_incomplete":
                    assert table.contains(s) and min(table.update_counts(s).values()) == 0
    assert covered > 0
    fallback_decisions = [d for d in decisions(trace) if d["payload"]["source"] == "queue_fallback"]
    for d in fallback_decisions:                                                 # grid and decisions agree
        q = d["payload"]["q_state"]
        cell = next(g for g in view["grids"] if g["green"] == q[2] and g["elapsed_bin"] == q[3])["cells"][q[0]][q[1]]
        assert cell["status"].startswith("fallback")


@pytest.mark.parametrize("which", ["fixed", "queue", "training", "frozen"])
def test_contract_G_report_markers_resolve_and_explain(runs, which):
    html = build_report(runs[which]).read_text()
    ids = {e["event_id"] for e in load_trace(runs[which]).events}
    anchors = set(re.findall(r'id="([^"]+)"', html))
    hrefs = set(re.findall(r'href="#([^"]+)"', html))
    assert hrefs and hrefs <= anchors <= ids
    assert "Explanation unavailable" not in html
    assert ('<table class="q"' in html) == (which in ("training", "frozen"))     # no fake grid for baselines


@pytest.mark.parametrize("which", ["fixed", "queue", "frozen"])
def test_module8_summary_metrics_match_independent_calculation(runs, which):
    trace = load_trace(runs[which])
    s = summarize(trace)
    obs = [e for e in trace.events if e["type"] == "observation" and 1 <= e["sim_time_s"] <= H]
    totals = [sum(e["payload"]["counts_by_direction"].values()) for e in obs]
    assert s["metrics"]["samples"] == H and not s["metrics"]["partial"]
    assert s["metrics"]["mean_queued_vehicles"] == sum(totals) / H
    assert s["metrics"]["peak_queued_vehicles"] == max(totals)
    assert sum(s["behavior"]["by_source"].values()) == s["behavior"]["decisions"] == len(decisions(trace))
    assert s["safety"] == {"overrides": 0, "violations": 0}


def test_contract_I_explain_and_summarize_cli(runs, capsys):
    run_dir = runs["training"]
    trace = load_trace(run_dir)
    d = next(e for e in decisions(trace) if e["payload"]["source"] == "q_greedy")
    assert main(["explain", "--run", str(run_dir), "--decision", d["event_id"]]) == 0
    assert main(["summarize", "--run", str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert "Alternative:" in out and "Decisions by source" in out
    assert (run_dir / "summary.json").is_file() and (run_dir / "metrics.csv").is_file()
