# traffic-cps

An explainable traffic signal controller for one simulated intersection. SUMO simulates the traffic; this project controls the light through direct TraCI, one simulated second at a time, and records and explains every observation, decision, light change, safety check, and learning update.

Three controllers run against identical traffic:

- **Fixed-time:** switches after a tuned green target.
- **Queue rule:** gives green to the direction with the higher queue score; ties keep the current green.
- **Tabular Q-learning:** learns a 200-state table of keep/switch values, then runs frozen for evaluation.

Every controller is subject to the same operating rules (2 s yellow, 5 s minimum green, 60 s green-service limit) and an independent safety monitor that checks every signal write.

## What makes it explainable

- **Faithful explanations.** Each decision is explained from the evidence that produced it, and a fidelity check re-runs the recorded rule on the recorded numbers before the explanation is shown. Q-learning decisions use the exact table the controller had at that moment, rebuilt from the run's starting table plus its logged updates.
- **"Why this rather than that."** Every decision states its alternative: the queue-rule threshold, the fixed-time switch point, or the nearest queue situation that would flip the learned choice.
- **Viewable without reading logs.** `explain` and `summarize` on the command line, and an offline HTML report per run with a queue and signal timeline, decision markers colored by source, and the learned policy as grids.

These checks show that explanations match the decision process. They do not show that people understand the controller better; no user study was run.

## Results

See [`docs/results.md`](docs/results.md). In short, on five held-out one-hour scenarios the queue rule had the lowest mean queue (5.12 vehicles), ahead of tuned fixed-time (9.59) and the 15-episode Q-learning policy (11.46). Low sensor noise had no measurable effect; every one of 23,487 generated explanations passed the fidelity check.

## Setup

SUMO 1.24.0 runs in a Linux container (the macOS 1.24.0 wheel needs Homebrew library versions that are no longer published). Requires Docker Desktop.

```sh
./dev.sh build                                  # build the image (rerun after dependency changes)
./dev.sh traffic-cps doctor                     # verify the pinned toolchain
```

Every command below runs inside the container through `./dev.sh`.

## Workflow

```sh
# Network and calibration (once)
traffic-cps scenario build --output scenarios/single
traffic-cps scenario saturation-check --config configs/default.yaml --network scenarios/single --seed 2 --output outputs/saturation_check_001

# A short smoke run, its explanation, summary, and report
traffic-cps scenario demand --network scenarios/single --seed 0 --profile balanced --horizon 60 --output scenarios/smoke
traffic-cps run --config configs/default.yaml --controller queue --scenario scenarios/smoke --horizon 60 --output outputs/smoke
traffic-cps explain --run outputs/smoke/runs/RUN_ID --decision EVENT_ID
traffic-cps summarize --run outputs/smoke/runs/RUN_ID
traffic-cps report --run outputs/smoke/runs/RUN_ID

# Full experiment
traffic-cps tune-fixed --config configs/default.yaml --suite development --output outputs/fixed_tuning_001
traffic-cps train --config configs/default.yaml --episodes 15 --output outputs/training_001
traffic-cps evaluate --config configs/default.yaml --checkpoint outputs/training_001/milestones/episode_015.json \
    --fixed-tuning outputs/fixed_tuning_001/fixed_tuning.json --suite final --output outputs/final_001
traffic-cps evaluate --config configs/default.yaml --checkpoint outputs/training_001/milestones/episode_015.json \
    --suite robustness --output outputs/robustness_001

# Resume training from the latest (or an explicitly selected older) checkpoint
traffic-cps train --config configs/default.yaml --resume outputs/training_001/checkpoints/training_latest.json --additional-episodes 5

# Tests
python -m pytest tests/unit
python -m pytest tests/integration                # real SUMO; requires a passing doctor
```

Exit codes: `0` success, `2` invalid configuration, checkpoint, or environment (nothing started), `1` runtime, safety, or persistence failure. Ctrl-C stops at the end of the current simulated second and saves completed learning.

## Outputs

`outputs/` is never committed and run directories are never overwritten. Each run folder holds the resolved configuration, scenario and run manifests, `policy_start.json`, the JSONL event log, SUMO's log, `report.html`, `summary.json`, and `metrics.csv`. A training folder adds `checkpoints/` (latest and previous), immutable `milestones/`, and development evaluations; experiment folders add `experiment.json` and `runs.csv` with per-seed results and aggregates.

## Documentation

- [`docs/traffic-explainability-design.md`](docs/traffic-explainability-design.md): the specification, including the standalone build contract and recorded implementation resolutions (Section K).
- [`docs/results.md`](docs/results.md): experiment results and limitations.
- [`docs/build-progress.md`](docs/build-progress.md): build history, decisions, and per-chunk results.
- [`docs/interview-design-qa.md`](docs/interview-design-qa.md): design rationale.

## Limits

One synthetic two-lane intersection with straight-through traffic only; no turns, pedestrians, all-red clearance, or real demand data. Discrete one-second simulation. Sensing reads simulator ground truth, except in the labeled noise experiment. Five evaluation scenarios give initial evidence, not statistical significance. Not suitable for road deployment.
