"""Chunk 7a with real SUMO: tuning, final comparison, robustness, vehicle accounting.

Module 8, Section H, Section I, Section J (fixed-time tuning, robustness). Short horizons and a reduced
seed/candidate set keep this fast; the full configured suite runs in chunk 7b.
"""

import json
from dataclasses import replace

import pytest

from traffic_cps import checkpoints as ck
from traffic_cps.cli import main
from traffic_cps.experiments import evaluate_final, evaluate_robustness, tune_fixed
from traffic_cps.explain import explain_event, load_trace
from traffic_cps.runtime import train
from tests.integration.harness import CONFIG, PROJECT_ROOT

H = 300
CFG = replace(CONFIG,
              signal=replace(CONFIG.signal, fixed_green_candidates_s=(20, 30)),
              experiment=replace(CONFIG.experiment, development_seeds=(100, 101), final_seeds=(200, 201),
                                 development_after_episodes=()))


def events_of(run_dir):
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


@pytest.fixture(scope="module")
def suite(tmp_path_factory):
    root = tmp_path_factory.mktemp("exp")
    tuning = tune_fixed(CFG, root / "tuning", horizon_s=H)
    trained = train(CFG, 2, root / "training", horizon_s=H)
    assert trained.ok
    checkpoint = root / "training" / "checkpoints" / ck.LATEST
    final = evaluate_final(CFG, checkpoint, root / "tuning" / "fixed_tuning.json", root / "final", horizon_s=H)
    robust = evaluate_robustness(CFG, checkpoint, root / "robust", horizon_s=H)
    return {"root": root, "tuning": tuning, "final": final, "robust": robust, "checkpoint": checkpoint}


# ----------------------------------------------------------------------
# Tuning
# ----------------------------------------------------------------------

def test_sectionJ_tuning_uses_only_development_seeds_and_records_every_candidate(suite):
    t = suite["tuning"]
    assert t["development_seeds"] == [100, 101] and set(t["candidates"]) == {20, 30}
    assert t["chosen_target_s"] in (20, 30) and all(r["complete"] for r in t["runs"])
    scenarios = sorted(p.name for p in (suite["root"] / "tuning" / "scenarios").iterdir())
    assert scenarios == ["seed_100", "seed_101"]                                  # never a final seed
    best = min(row["mean"] for row in t["candidates"].values())
    assert t["candidates"][t["chosen_target_s"]]["mean"] == best
    assert "not a webster" in t["method"].lower()


# ----------------------------------------------------------------------
# Final comparison
# ----------------------------------------------------------------------

def test_module8_final_comparison_runs_three_policies_on_identical_demand(suite):
    f = suite["final"]
    runs = f["runs"]
    assert {(r["policy"], r["seed"]) for r in runs} == {(p, s) for p in ("fixed", "queue", "q_learning")
                                                        for s in (200, 201)}
    assert all(r["complete"] for r in runs) and f["incomplete_runs"] == []
    root = suite["root"] / "final" / "runs"
    for seed in (200, 201):
        hashes = {json.loads((root / f"final-{p}-seed{seed:03d}" / "run.manifest.json").read_text())
                  ["scenario_manifest_sha256"] for p in ("fixed", "queue", "q_learning")}
        assert len(hashes) == 1                                                   # one materialized demand file
    fixed_decisions = [e for e in events_of(root / "final-fixed-seed200") if e["type"] == "decision"]
    assert fixed_decisions and {e["payload"]["fixed_green_target_s"] for e in fixed_decisions} == \
        {suite["tuning"]["chosen_target_s"]}
    assert f["checkpoint"]["checkpoint_id"] == json.loads(suite["checkpoint"].read_text())["checkpoint_id"]


def test_module8_final_aggregates_report_mean_sd_and_paired_differences(suite):
    agg = suite["final"]["aggregates"]["mean_queued_vehicles"]
    assert set(agg["policies"]) == {"fixed", "queue", "q_learning"}
    assert all(p["n"] == 2 and p["sd"] is not None for p in agg["policies"].values())
    for key in ("q_learning_vs_fixed", "q_learning_vs_queue", "queue_vs_fixed"):
        assert agg["paired"][key]["seeds"] == [200, 201]
    assert (suite["root"] / "final" / "runs.csv").is_file() and (suite["root"] / "final" / "experiment.json").is_file()


def test_module8_every_generated_explanation_passes_fidelity(suite):
    for record in (suite["final"], suite["robust"]):
        for r in record["runs"]:
            f = r["fidelity"]
            assert f["explained"] > 0 and f["mismatch"] == 0 and f["unavailable"] == 0 and f["errors"] == []


def test_sectionH_every_vehicle_classified_exactly_once(suite):
    for run_dir in (suite["root"] / "final" / "runs").iterdir():
        end = events_of(run_dir)[-1]["payload"]
        acc = end["vehicle_accounting"]
        assert acc["consistent"] and acc["status"] == "complete" and not acc["unexpected_ids"]
        ids = acc["arrived_ids"] + acc["active_ids"] + acc["not_inserted_ids"]
        assert len(ids) == len(set(ids)) == acc["scheduled"]
        summary = json.loads((run_dir / "summary.json").read_text())
        assert summary["metrics"]["completed_vehicles"] == acc["arrived"]


# ----------------------------------------------------------------------
# Robustness
# ----------------------------------------------------------------------

def run_dir(suite, policy, level, seed=200):
    return suite["root"] / "robust" / "runs" / f"robust-{policy}-{level}-seed{seed:03d}"


def test_sectionH_both_fault_levels_use_the_same_noise_seed_per_scenario(suite):
    for seed in (200, 201):
        seeds = {json.loads((run_dir(suite, "queue", lvl, seed) / "run.manifest.json").read_text())
                 ["measurement"]["seed"] for lvl in ("low", "high")}
        assert seeds == {CFG.robustness.noise_seed_offset + seed}


def test_sectionH_truth_and_measurement_kept_separate(suite):
    clean = [e["payload"] for e in events_of(run_dir(suite, "queue", "clean")) if e["type"] == "observation"]
    assert all(p["counts_by_lane"] == p["true_counts_by_lane"] and p["fault_label"] == "clean" for p in clean)
    noisy = [e["payload"] for e in events_of(run_dir(suite, "queue", "high")) if e["type"] == "observation"]
    assert noisy[0]["counts_by_lane"] == noisy[0]["true_counts_by_lane"]          # t = 0 delivers truth
    assert any(p["counts_by_lane"] != p["true_counts_by_lane"] for p in noisy)
    s = json.loads((run_dir(suite, "queue", "high") / "summary.json").read_text())
    true_total = sum(sum(p["true_counts_by_direction"].values()) for p in noisy[1:])
    assert s["metrics"]["metric_counts"] == "true" and s["metrics"]["queue_exposure_vehicle_seconds"] == true_total


def test_sectionH_policy_inputs_use_measured_counts(suite):
    events = events_of(run_dir(suite, "queue", "high"))
    obs = {e["event_id"]: e["payload"] for e in events if e["type"] == "observation"}
    decisions = [e["payload"] for e in events if e["type"] == "decision"]
    differing = [d for d in decisions if obs[d["observation_id"]]["counts_by_direction"]
                 != obs[d["observation_id"]]["true_counts_by_direction"]]
    assert differing
    for d in differing:
        assert d["counts"] == obs[d["observation_id"]]["counts_by_direction"]     # the controller saw measured


def test_sectionH_explanations_show_seen_versus_true(suite):
    trace = load_trace(run_dir(suite, "queue", "high"))
    obs = {e["event_id"]: e["payload"] for e in trace.events if e["type"] == "observation"}
    d = next(e for e in trace.events if e["type"] == "decision" and obs[e["payload"]["observation_id"]]
             ["counts_by_direction"] != obs[e["payload"]["observation_id"]]["true_counts_by_direction"])
    text = explain_event(trace, d["event_id"]).text
    assert "the controller saw" in text and "the simulator's true counts were" in text


def test_sectionH_robustness_records_degradation_and_diagnostics(suite):
    r = suite["robust"]
    for policy in ("queue", "q_learning"):
        paired = r["degradation_vs_clean"][policy]["paired"]
        assert set(paired) == {"low_vs_clean", "high_vs_clean"}
    assert set(r["diagnostics"]) == {f"{p}/{lvl}/seed{s:03d}" for p in ("queue", "q_learning")
                                     for lvl in ("clean", "low", "high") for s in (200, 201)}
    assert all(d["phantom_demand_events"] == 0 and d["masked_demand_seconds"] == 0
               for k, d in r["diagnostics"].items() if "/clean/" in k)               # truth cannot disagree


def test_contract_I_evaluate_final_requires_fixed_tuning(suite, tmp_path):
    assert main(["evaluate", "--config", str(PROJECT_ROOT / "configs/default.yaml"), "--checkpoint",
                 str(suite["checkpoint"]), "--suite", "final", "--output", str(tmp_path / "x")]) == 2
    assert not (tmp_path / "x").exists()
