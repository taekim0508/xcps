"""Experiment runs. Chunk 2 adds only the peak saturation check (build contract, Section B).

The check runs the queue controller on one peak seed and measures, inside the
peak window [H/3, 2H/3), the fraction of free-choice checks at which both
direction scores are at their cap (2.0 within the tie tolerance). If more
than the configured fraction is saturated, the peak demand is too heavy to be
informative and must be lowered, recorded, and regenerated.
"""

from __future__ import annotations

import json
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
