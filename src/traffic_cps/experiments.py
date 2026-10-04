"""Experiment runs (Module 8, build contract Sections B, H, I).

- Peak saturation check (Section B).
- Fixed-time tuning on development seeds only (`tune_fixed`).
- Final comparison of fixed-time (tuned), queue rule, and frozen Q-learning on the final seeds,
  with each seed's demand materialized once and reused by every policy (`evaluate_final`).
- Sensing robustness: queue rule and frozen Q-learning, clean and per fault level, on the final
  seeds (`evaluate_robustness`).
- Aggregates: per-seed values, mean, sample SD, paired differences against baselines; incomplete
  runs are counted and reported, never averaged. Every run gets its report and summary, and every
  generated explanation is re-checked (fidelity sweep).

The saturation check runs the queue controller on one peak seed and measures, inside the
peak window [H/3, 2H/3), the fraction of free-choice checks at which both
direction scores are at their cap (2.0 within the tie tolerance). If more
than the configured fraction is saturated, the peak demand is too heavy to be
informative and must be lowered, recorded, and regenerated.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from traffic_cps.config import Config
from traffic_cps.contracts import CompletionStatus
from traffic_cps.controllers.queue import QueueController
from traffic_cps.events import EVENTS_FILE, write_json_exclusive
from traffic_cps.runtime import Runtime
from traffic_cps.scenario import (
    GENERATOR_VERSION,
    PEAK_CALIBRATION_FILE,
    DemandProfile,
    create_scenario,
    direction_probabilities,
)


class ExperimentError(RuntimeError):
    pass


def saturation_check(config: Config, network_dir: Path | str, seed: int, output_dir: Path | str,
                     horizon_s: int | None = None, record_in_network: bool = True) -> dict:
    network_dir, output_dir = Path(network_dir), Path(output_dir)
    if seed in config.experiment.final_seeds:
        raise ExperimentError(f"seed {seed} is a final evaluation seed; calibration must not use final seeds")
    horizon = config.simulation.horizon_s if horizon_s is None else horizon_s
    scenario = output_dir / "scenario"
    create_scenario(network_dir, scenario, seed, DemandProfile.PEAK, horizon)
    result = Runtime(config, scenario, QueueController(), output_dir, horizon_s=horizon).run()
    if result.completion is not CompletionStatus.COMPLETED:
        raise ExperimentError(f"saturation-check run {result.run_id} did not complete: {result.failure}")

    decisions = []
    for line in (Path(result.run_dir) / EVENTS_FILE).read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        t = event["sim_time_s"]
        if event["type"] == "decision" and 3 * t >= horizon and 3 * t < 2 * horizon:  # peak window
            decisions.append(event["payload"])
    if not decisions:
        raise ExperimentError("no free-choice checks inside the peak window")
    saturated = sum(1 for d in decisions if d["both_at_cap"])
    fraction = saturated / len(decisions)
    threshold = config.robustness.saturation_check_max_fraction
    peak_p = direction_probabilities(DemandProfile.PEAK, (horizon + 2) // 3, horizon)[0]
    record = {
        "check": "peak_saturation",
        "generator_version": GENERATOR_VERSION,
        "peak_probability_per_direction": peak_p,
        "traffic_seed": seed,
        "horizon_s": horizon,
        "peak_window_s": [-(-horizon // 3), -(-2 * horizon // 3)],
        "free_choice_checks_in_window": len(decisions),
        "both_scores_at_cap": saturated,
        "fraction": fraction,
        "max_fraction": threshold,
        "passed": fraction <= threshold,
        "run_id": result.run_id,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json_exclusive(output_dir / "saturation_check.json", record)
    if record_in_network:
        write_json_exclusive(network_dir / PEAK_CALIBRATION_FILE, record)
    return record


# ----------------------------------------------------------------------
# Pure helpers: seed discipline, tuning choice, aggregates
# ----------------------------------------------------------------------

TIE = 1e-9


def development_only(seeds, final) -> list[int]:
    """Tuning must never read a final seed (Section J)."""
    leaked = sorted(set(seeds) & set(final))
    if leaked:
        raise ExperimentError(f"tuning would read final seeds {leaked}; tuning uses development seeds only")
    return list(seeds)


def choose_fixed_target(results: dict[int, list[float | None]]) -> tuple[int, dict]:
    """Lowest mean over development seeds among candidates with every run complete; ties to shorter."""
    table = {}
    for candidate, values in sorted(results.items()):
        incomplete = sum(v is None for v in values)
        done = [v for v in values if v is not None]
        table[candidate] = {"values": values, "incomplete_seeds": incomplete, "eligible": incomplete == 0 and bool(done),
                            "mean": (sum(done) / len(done)) if done else None}
    eligible = [(c, t["mean"]) for c, t in table.items() if t["eligible"]]
    if not eligible:
        raise ExperimentError("no candidate completed every development run; nothing can be chosen")
    best = min(m for _, m in eligible)
    chosen = min(c for c, m in eligible if abs(m - best) <= TIE)
    return chosen, table


def _sd(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def aggregate_metric(per_seed: dict[str, dict[int, float | None]], baselines: tuple[str, ...]) -> dict:
    """Mean and sample SD per policy over complete runs; paired differences (policy - baseline) on seeds
    where both completed; relative change only where the baseline value is nonzero."""
    policies = {}
    for policy, values in per_seed.items():
        done = {s: v for s, v in values.items() if v is not None}
        policies[policy] = {"by_seed": values, "n": len(done),
                            "incomplete_seeds": sorted(s for s, v in values.items() if v is None),
                            "mean": (sum(done.values()) / len(done)) if done else None,
                            "sd": _sd(list(done.values()))}
    paired = {}
    for baseline in baselines:
        for policy in per_seed:
            if policy == baseline:
                continue
            seeds = sorted(s for s in per_seed[policy] if per_seed[policy][s] is not None
                           and per_seed[baseline].get(s) is not None)
            diffs = {s: per_seed[policy][s] - per_seed[baseline][s] for s in seeds}
            base_mean = (sum(per_seed[baseline][s] for s in seeds) / len(seeds)) if seeds else None
            pol_mean = (sum(per_seed[policy][s] for s in seeds) / len(seeds)) if seeds else None
            paired[f"{policy}_vs_{baseline}"] = {
                "seeds": seeds, "differences": diffs,
                "mean_difference": (sum(diffs.values()) / len(diffs)) if diffs else None,
                "sd_difference": _sd(list(diffs.values())),
                "relative_change_by_seed": {s: (diffs[s] / per_seed[baseline][s]) if per_seed[baseline][s] != 0
                                            else None for s in seeds},
                "relative_change_of_means": ((pol_mean - base_mean) / base_mean) if base_mean else None,
            }
    return {"policies": policies, "paired": paired}


# ----------------------------------------------------------------------
# Running suites
# ----------------------------------------------------------------------

def _fidelity_sweep(run_dir: Path) -> dict:
    """Re-explain every explainable event and count verified / mismatched / unavailable (Module 8)."""
    from traffic_cps.explain import ExplainError, explain_event, load_trace

    trace = load_trace(run_dir)
    counts = {"explained": 0, "verified": 0, "mismatch": 0, "unavailable": 0, "not_applicable": 0, "errors": []}
    for e in trace.events:
        if e["type"] not in ("decision", "operating_action", "phantom_demand", "masked_demand",
                             "safety_override", "safety_violation"):
            continue
        try:
            x = explain_event(trace, e["event_id"])
        except ExplainError as exc:
            counts["errors"].append(f"{e['event_id']}: {exc}")
            continue
        counts["explained"] += 1
        status = x.facts.get("fidelity", "not_applicable")
        counts[status] = counts.get(status, 0) + 1
    counts["trace_complete"] = trace.complete
    return counts


def _run(config: Config, scenario: Path, controller, out: Path, horizon: int, run_id: str, *, learner=None,
         measurement=None, provenance=None, notify=None) -> dict:
    from traffic_cps.contracts import Mode
    from traffic_cps.report import build_report, summarize, write_summary
    from traffic_cps.explain import load_trace

    result = Runtime(config, scenario, controller, out, horizon_s=horizon, mode=Mode.EVALUATE, learner=learner,
                     measurement=measurement, provenance=provenance, notify=notify, run_id=run_id).run()
    record = {"run_id": result.run_id, "run_dir": result.run_dir, "completion": result.completion.value,
              "persistence_ok": result.persistence_ok, "failure": result.failure, "outputs_ok": True}
    try:
        build_report(result.run_dir)
        write_summary(result.run_dir)
        record["summary"] = summarize(load_trace(result.run_dir))
        record["fidelity"] = _fidelity_sweep(Path(result.run_dir))
    except Exception as exc:  # reported separately from simulation completion (Section I)
        record["outputs_ok"] = False
        record["outputs_failure"] = f"{type(exc).__name__}: {exc}"
    record["complete"] = (result.completion is CompletionStatus.COMPLETED and result.persistence_ok
                          and record["outputs_ok"])
    return record


def _scenario_for(config: Config, out: Path, seed: int, index: int, horizon: int) -> Path:
    from traffic_cps.scenario import profile_for_index

    path = out / "scenarios" / f"seed_{seed:03d}"
    create_scenario(config.network.directory, path, seed, profile_for_index(index), horizon)
    return path


def _metric(record: dict, key: str = "mean_queued_vehicles"):
    return record["summary"]["metrics"][key] if record.get("complete") else None


def tune_fixed(config: Config, output_dir: Path | str, horizon_s: int | None = None, notify=None) -> dict:
    from traffic_cps.controllers.fixed import FixedTimeController

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=False)
    horizon = config.simulation.horizon_s if horizon_s is None else horizon_s
    seeds = development_only(config.experiment.development_seeds, config.experiment.final_seeds)
    scenarios = {seed: _scenario_for(config, out, seed, i, horizon) for i, seed in enumerate(seeds)}
    runs, results = [], {}
    for candidate in config.signal.fixed_green_candidates_s:
        results[candidate] = []
        for seed in seeds:
            r = _run(config, scenarios[seed], FixedTimeController(candidate), out, horizon,
                     f"tune-fixed{candidate:02d}-seed{seed:03d}", notify=notify,
                     provenance={"purpose": "fixed_tuning", "traffic_seed": seed, "candidate_s": candidate})
            r.update(candidate_s=candidate, seed=seed)
            runs.append(r)
            results[candidate].append(_metric(r))
    chosen, table = choose_fixed_target(results)
    record = {
        "method": "grid search on mean queued vehicles (true counts) averaged over development seeds; ties go to "
                  "the shorter target. Not a Webster-formula derivation.",
        "development_seeds": seeds, "horizon_s": horizon, "candidates": table, "chosen_target_s": chosen,
        "green_service_limit_s": config.signal.green_service_limit_s,
        "runs": [{k: r[k] for k in ("run_id", "candidate_s", "seed", "completion", "complete", "failure")}
                 for r in runs],
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json_exclusive(out / "fixed_tuning.json", record)
    return record


def _load_tuning(config: Config, path: Path) -> tuple[int, str]:
    from traffic_cps.config import sha256_file

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        target = data["chosen_target_s"]
        seeds = data["development_seeds"]
    except (OSError, ValueError, KeyError) as exc:
        raise ExperimentError(f"cannot read fixed tuning {path}: {exc}") from exc
    development_only(seeds, config.experiment.final_seeds)
    if isinstance(target, bool) or not isinstance(target, int) or not 0 < target < config.signal.green_service_limit_s:
        raise ExperimentError(f"tuned target {target!r} must be a positive integer below the service limit")
    return target, sha256_file(path)


def _frozen_learner(config: Config, checkpoint: Path, scenario: Path, horizon: int):
    from traffic_cps.checkpoints import CheckpointStore, compatibility, file_sha256, learner_from_checkpoint
    from traffic_cps.scenario import SCENARIO_MANIFEST

    network = json.loads((scenario / SCENARIO_MANIFEST).read_text(encoding="utf-8"))["network"]
    data = CheckpointStore(checkpoint.parent).load(checkpoint, compatibility(config, network, horizon), "evaluate")
    return learner_from_checkpoint(config, data), {
        "checkpoint_path": str(checkpoint), "checkpoint_id": data["checkpoint_id"],
        "checkpoint_file_sha256": file_sha256(checkpoint), "integrity_sha256": data["integrity_sha256"]}


def _write_experiment(out: Path, record: dict, runs: list[dict]) -> None:
    import csv

    from traffic_cps.report import METRICS_FILE

    write_json_exclusive(out / "experiment.json", record)
    rows = []
    for r in runs:
        metrics_path = Path(r["run_dir"]) / METRICS_FILE
        if metrics_path.is_file():
            with metrics_path.open(encoding="utf-8") as fh:
                rows += [dict(row, complete=r["complete"]) for row in csv.DictReader(fh)]
    if rows:
        with (out / "runs.csv").open("x", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def _suite_record(kind: str, horizon: int, runs: list[dict], extra: dict) -> dict:
    return {
        "suite": kind, "horizon_s": horizon, **extra,
        "runs": [{k: r.get(k) for k in ("run_id", "policy", "seed", "level", "completion", "complete", "failure",
                                        "outputs_ok", "fidelity")} for r in runs],
        "incomplete_runs": [{"run_id": r["run_id"], "failure": r.get("failure") or r.get("outputs_failure")}
                            for r in runs if not r["complete"]],
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


METRICS_COMPARED = ("mean_queued_vehicles", "peak_queued_vehicles", "queue_exposure_vehicle_seconds")


def evaluate_final(config: Config, checkpoint: Path | str, fixed_tuning: Path | str, output_dir: Path | str,
                   horizon_s: int | None = None, notify=None) -> dict:
    from traffic_cps.controllers.fixed import FixedTimeController
    from traffic_cps.controllers.q_learning import QLearningController
    from traffic_cps.controllers.queue import QueueController

    out = Path(output_dir)
    horizon = config.simulation.horizon_s if horizon_s is None else horizon_s
    target, tuning_sha = _load_tuning(config, Path(fixed_tuning))
    out.mkdir(parents=True, exist_ok=False)
    runs = []
    for index, seed in enumerate(config.experiment.final_seeds):
        scenario = _scenario_for(config, out, seed, index, horizon)   # materialized once, reused by all three
        learner, ident = _frozen_learner(config, Path(checkpoint), scenario, horizon)
        for policy, controller, kwargs in (
                ("fixed", FixedTimeController(target), {}),
                ("queue", QueueController(), {}),
                ("q_learning", QLearningController(learner), {"learner": learner})):
            r = _run(config, scenario, controller, out, horizon, f"final-{policy}-seed{seed:03d}", notify=notify,
                     provenance={"purpose": "final_comparison", "traffic_seed": seed, "fixed_target_s": target,
                                 **(ident if policy == "q_learning" else {})}, **kwargs)
            r.update(policy=policy, seed=seed)
            runs.append(r)
    aggregates = {m: aggregate_metric({p: {r["seed"]: _metric(r, m) for r in runs if r["policy"] == p}
                                       for p in ("fixed", "queue", "q_learning")}, baselines=("fixed", "queue"))
                  for m in METRICS_COMPARED}
    q_runs = [r for r in runs if r["policy"] == "q_learning" and r["complete"]]
    record = _suite_record("final", horizon, runs, {
        "final_seeds": list(config.experiment.final_seeds), "fixed_target_s": target,
        "fixed_tuning": str(fixed_tuning), "fixed_tuning_sha256": tuning_sha,
        "checkpoint": ident,
        "aggregates": aggregates,
        "q_learning_fallback_share_by_seed": {r["seed"]: r["summary"]["behavior"]["fallback_share_of_decisions"]
                                              for r in q_runs},
    })
    _write_experiment(out, record, runs)
    return record


def evaluate_robustness(config: Config, checkpoint: Path | str, output_dir: Path | str,
                        horizon_s: int | None = None, notify=None) -> dict:
    from traffic_cps.controllers.q_learning import QLearningController
    from traffic_cps.controllers.queue import QueueController
    from traffic_cps.faults import SensorNoise

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=False)
    horizon = config.simulation.horizon_s if horizon_s is None else horizon_s
    lanes = tuple(lane for d in ("NS", "WE") for lane in config.network.lanes[d])
    levels = ["clean", *config.robustness.levels]
    runs, ident = [], None
    for index, seed in enumerate(config.experiment.final_seeds):
        scenario = _scenario_for(config, out, seed, index, horizon)
        for policy in ("queue", "q_learning"):
            for level in levels:
                noise = None
                if level != "clean":
                    spec = config.robustness.levels[level]
                    noise = SensorNoise(level, spec.noise_bound_vehicles, spec.stale_probability,
                                        config.robustness.noise_seed_offset + seed, lanes)
                kwargs = {}
                if policy == "q_learning":
                    learner, ident = _frozen_learner(config, Path(checkpoint), scenario, horizon)
                    controller, kwargs = QLearningController(learner), {"learner": learner}
                else:
                    controller = QueueController()
                r = _run(config, scenario, controller, out, horizon, f"robust-{policy}-{level}-seed{seed:03d}",
                         measurement=noise, notify=notify,
                         provenance={"purpose": "robustness", "traffic_seed": seed, "fault_level": level,
                                     **(ident if policy == "q_learning" else {})}, **kwargs)
                r.update(policy=policy, seed=seed, level=level)
                runs.append(r)
    aggregates = {}
    for policy in ("queue", "q_learning"):
        per_level = {level: {r["seed"]: _metric(r) for r in runs if r["policy"] == policy and r["level"] == level}
                     for level in levels}
        aggregates[policy] = aggregate_metric(per_level, baselines=("clean",))
    diagnostics = {f"{r['policy']}/{r['level']}/seed{r['seed']:03d}": r["summary"]["robustness"]
                   for r in runs if r.get("summary")}
    record = _suite_record("robustness", horizon, runs, {
        "final_seeds": list(config.experiment.final_seeds), "levels": {
            name: {"noise_bound_vehicles": spec.noise_bound_vehicles, "stale_probability": spec.stale_probability}
            for name, spec in config.robustness.levels.items()},
        "noise_seed_offset": config.robustness.noise_seed_offset, "checkpoint": ident,
        "degradation_vs_clean": aggregates, "diagnostics": diagnostics,
    })
    _write_experiment(out, record, runs)
    return record
