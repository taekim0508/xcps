# Build Progress

Tracks the 8 development chunks and the current position. Chunks follow the design doc's "Implementation order" (steps 0 to 7) exactly. The design doc remains the specification; this file only records status and decisions.

**Current chunk:** 7 (awaiting review)

Status values: `not started`, `in progress`, `awaiting review`, `done`.

## Chunks

| # | Chunk | Status | Gate |
|---|---|---|---|
| 0 | Foundation: scaffold, environment, scenario | done | `doctor` passes on 1.24.0; `scenario build` produces a validated network |
| 1 | Simulator interface and safety | done | Real SUMO steps under the monitor with zero violations; fault-injection cases pass |
| 2 | Thin vertical slice | done | `run` -> `explain` -> `report.html` on one short run; peak saturation check recorded |
| 3 | Fixed-time and full runtime enforcement | done | Full-horizon fixed and queue runs with correct attribution |
| 4 | Tabular Q-learning | done | Short training learns; frozen evaluation leaves the table unchanged |
| 5 | Persistence and failure-safe shutdown | done | Train -> interrupt -> resume -> evaluate cycle works |
| 6 | Full explanation layer | done | Fidelity checks pass on all three controllers |
| 7 | Experiments and results | awaiting review | 7a: code and tests pass. 7b: tuning, training, comparison, robustness runs, honest results summary |

## Chunk details

### 0. Foundation: scaffold, environment, scenario
- Files: `pyproject.toml`, `README.md` (stub), `configs/default.yaml`, `src/traffic_cps/{__init__,__main__,cli,config,contracts,scenario}.py`, `requirements-lock.txt`
- Also: `Dockerfile`, `dev.sh`, `.dockerignore` (container runtime, see Decisions)
- Tests: every Section C config rejection rule; byte-identical demand XML for a fixed seed; profile cycling by seed; network has exactly 4 movements and positive generated lane lengths (integration)
- Result: 79 unit + 7 integration tests pass in the container; `doctor` passes; `scenarios/single` and `scenarios/smoke` generated. Generated incoming lane lengths: N/S 246.00 m, W/E 239.60 m.
- Moved to chunk 1: capacity arithmetic (`C = L / 7.5`) belongs to the sensor.

#### Chunk 0 in plain terms
- **Environment.** The `Dockerfile` is the recipe for the environment SUMO *runs* in (SUMO is not compiled here): a minimal Linux with Python 3.11.17, five system libraries SUMO loads at startup, then SUMO 1.24.0 and our Python packages. Docker builds the recipe once into an image (a saved snapshot); every `./dev.sh` command starts a container (a short-lived running copy of that image) with the project folder shared into it. SUMO's Linux build is used because the macOS build needs library versions that are no longer available.
- **Project skeleton.** `pyproject.toml` defines the package, pinned dependencies, and the `traffic-cps` command. `doctor` verifies tool and package versions before anything runs.
- **Configuration.** A strict loader validates `configs/default.yaml` (unknown, missing, mistyped, or rule-breaking values stop the program) and turns it into immutable records. A hash (content fingerprint) of the resolved settings changes if any value changes, so each run can prove which settings it used.
- **Network.** `scenario.py` writes three small description files: 5 points (`J0` at the center, `N`/`S`/`W`/`E` 250 m out, x,y in meters), 4 one-way two-lane roads, and the 4 allowed lane-to-lane connections (straight through only). `netconvert` (a SUMO tool) compiles them into `network.net.xml`; the code then reads that file back and validates it (one signalized junction, exactly the 4 movements, positive lane lengths) instead of trusting it. Result: two-lane, one-way roads running north to south and west to east, crossing at one traffic light.
- **Traffic.** A seeded random generator produces the cars, so the same seed always gives identical arrivals and controllers can be compared on exactly the same traffic. The traffic file (`demand.rou.xml`) defines the vehicle type (length, acceleration, braking, max speed, driver imperfection), the two routes, and each car individually (ID, route, departure second, starting lane). So far only the 60 s smoke scenario exists; full-hour experiment scenarios are generated in later chunks.
- **Manifests.** Each generated network and scenario gets a manifest: a record of its inputs (seed, profile, horizon, every scheduled car) and the hash of each file, so any result traces back to exactly what produced it and altered files are detectable.
- **How SUMO gets its inputs (design; live control is built in chunks 1 to 3).** At launch SUMO receives the network file, the traffic file, and startup options on its command line. While running, our Python adapter uses TraCI (SUMO's remote-control interface) to advance time one second at a time, read lane queues and light colors, and set the traffic light. Only the adapter talks to SUMO.

### 1. Simulator interface and safety
- Files: `adapter.py`, `sensor.py`, `safety.py`, `faults.py` (test injection only)
- Approved choices: sensor owns the same-second retry loop; faults are injected by wrapping the real TraCI connection; S4 deadline is t+1 (design doc Section K). Visual mode (`sumo-gui`) skipped for now.
- Tests: capacity `L / 7.5` and lane/direction scores; masks derived from `getControlledLinks`; exact one-second advance; startup exception; vetoes for direct opposing green, premature yellow, late forced switch; same-time read retries (3 attempts max) then explicit failure; queue score and capacity arithmetic

- Result: 124 unit + 29 integration tests pass in the container. Full-hour peak run (seed 2, switch every 30 s) under the monitor: 3,601 observations, 102 switches, 0 violations, 0 overrides, 0 S4 misses, 3.4 s wall time.
- Test correction: the yellow-capture test first expected the t=10 primary observation to show yellow. Per Section E the observation (step 3) precedes the request (step 6), so the yellow is captured by the t=10 signal event and the color-onset time; the test now checks that.
- Not in chunk 1: actuator, controllers, event log file, runtime loop (chunk 2). Records are in memory only.

### 2. Thin vertical slice
- Files: `controllers/__init__.py`, `controllers/queue.py`, `actuator.py`, `runtime.py` (minimal), `events.py` (minimal), `explain.py` (minimal), `report.py` (timeline), `cli.py` (`run`, `explain`, `report`)
- Tests: queue rule with `1e-9` tolerance and current-green tie wins; actuator accepted/blocked; 10s -> 12s -> 20s timeline; 60s smoke run end to end
- Then: peak saturation check; regenerate demand and record in manifest if > 25%
- Approved choices: runtime already includes the step-5 forced switch and per-second S4 check (needed for an honest saturation check); saturation check is a CLI subcommand `scenario saturation-check` (addition to Section I) so its result is recorded; explainer/report cover only queue decisions, holds, forced switches, and safety events for now.

- Result: 167 unit + 34 integration tests pass. CLI smoke path (`run` -> `explain` -> `report`) exits 0 on `scenarios/smoke`.
- Peak saturation check (seed 2, H=3600): PASS, 0/126 free choices in the peak window had both scores at cap; recorded in `scenarios/single/peak_calibration.json`. No demand change needed.
- Finding for chunk 4: peak queues stay far below capacity under the queue rule (max 20 waiting on N/S, max direction score 0.61 of 2.0; it switches about every 10 s). If this holds for Q-learning, only the two lowest queue bins ([0, 0.4), [0.4, 0.8)) will be visited. Not a spec violation; flag when planning chunk 4.
- Not yet: `scenarios/smoke` was generated before calibration, so its manifest has an empty `calibration` list (scenarios generated from now on include it). Fixed-time controller, failure-path tests, and the full Module 5 checklist are chunk 3.

### 3. Fixed-time and full runtime enforcement
- Files: `controllers/fixed.py`, `runtime.py` (full Section E ordering), `cli.py`
- Tests: `fixed_schedule` attribution (zero forced switches in a normal full-hour fixed run); target >= service limit rejected; service-limit operating action plus S4; 3,601 observations and 720 scheduled checks; no command at or after H; failure paths (sensor, command, safety violation, simulator advance, event-log write) stop as incomplete with exit 1
- Approved choices: fixed-time switches when elapsed green >= target, regardless of competing demand; an unexpected block also logs an `error` event (stage `unexpected_block`, recovered); checkpoint and interrupt failures stay in chunk 5. "Reward once per second" moved to chunk 4, where the feedback interval and its consumer (the learner) are built together.

- Result: 183 unit + 62 integration tests pass in the container. Full-hour peak runs (seed 2, H=3600): fixed (target 30 s) 3,601 observations, 720 checks, 102 switches, all `fixed_schedule`, 0 operating actions, 0 violations; queue 3,601 observations, 720 checks, 313 switches, 0 operating actions, 0 violations. About 5 s wall time each.
- Fixed-time realized greens: first 30 s, then 33 s. After the first switch every green starts 2 s after a check, so the first check at or past the target is 3 s late. Every candidate (max 55 s) therefore switches by 58 s, below the 60 s limit.
- Finding: the queue rule never reaches the service limit at current demand. Probe runs (queue, H=3600, seeds 0 to 2 of balanced, asymmetric, and peak): 0 forced switches in all 9; longest green before a queue switch 43 s. The first full-hour queue test checked forced switches in a loop that ran zero times; it was replaced by a full-hour asymmetric run with a never-switching test controller (every switch forced, at least 10 required), so the check cannot pass empty.
- Schema fix during testing: the unexpected-block diagnostic first serialized its origin as `origin_id`; it now uses the shared `{kind, id}` form via `events.unexpected_block_payload`.

### 4. Tabular Q-learning
- Files: `learning.py`, `controllers/q_learning.py`, `runtime.py` (train/evaluate), `cli.py` (`train`, basic)
- Approved choices: (1) no table file until chunk 5; frozen evaluation takes the table in memory (tests only) and the event log (`policy_start.json` plus ordered `learning_update` events) already reconstructs it; `run --controller q_learning --checkpoint` arrives with chunk 5. (2) Evaluation tracks no feedback windows and emits no feedback events; per-second outcomes are in the observations, and `learning_update` always means the table changed. (3) Development evaluations at episodes 5/10/15 move to chunk 5 (they need milestone files). Exploration draw order: one `random()` for explore-or-not, then `choice()` only when exploring.
- Tests: reward added once per second to the pending interval (Section J); bin edges; hand-computed Q-update; counts only on completed updates; epsilon progress; coverage fallback; feedback discard at cutoff/failure; frozen evaluation never updates

- Result: 241 unit + 78 integration tests pass in the container. `train --episodes 2 --horizon 300` exits 0; the second episode's `policy_start.json` is rebuilt exactly from the first episode's start plus its ordered updates (old values validated).
- Probe (not an experiment result): 3 full-hour training episodes (seeds 0 to 2) took 14 s in total; 1,467 updates, 2 discarded at cutoffs, epsilon 1.0 -> 0.48; 23 states, 19 with both actions updated. A frozen evaluation on development seed 100 used the learned values for all 441 free choices (0 fallbacks).
- Revises the chunk 2 bin concern: under mostly random exploration, queues grow larger than under the queue rule, and updates reached queue bins 0 to 3 (not only 0 and 1). Coverage under the final 15-episode budget is still to be measured in chunk 7.
- Not in chunk 4: table/checkpoint files, resume, milestone development evaluations (chunk 5); counterfactuals, historical-revision replay in explanations, global policy view (chunk 6).

### 5. Persistence and failure-safe shutdown
- Files: `checkpoints.py`, `events.py` (all event types, manifests, `policy_start.json`), `runtime.py` (shutdown), `cli.py` (`--resume`, milestones, seed allocation)
- Approved choices: (1) Ctrl-C sets a stop flag checked at the tick boundary; then unfinished feedback is discarded, a shutdown checkpoint is saved, exit 1. (2) Resume requires the same horizon; frozen evaluation may use any valid horizon and records it. (3) A failed development evaluation is reported separately, training continues, and `train` exits 1 at the end. (4) Version compatibility: SUMO, TraCI, sumolib exact; Python major.minor. Also in this chunk: milestone development evaluations (from chunk 4) and `run --controller q_learning --checkpoint`. Built in two internal steps: (a) checkpoints, resume, shutdown, failure policy; (b) milestones and development evaluations.
- Tests: zero entries, RNG, counters round-trip; missing/corrupt/incompatible rejection; latest/previous rotation; interrupted write; log and checkpoint write failures give correct exit codes

- Result: 268 unit + 95 integration tests pass in the container. End to end with the default config (H=3600): `train --episodes 5` exit 0 in 44 s (5 episodes, checkpoints through generation 28, milestone `episode_005.json`, development evaluations on seeds 100 to 102 all completed); `train --resume ... --additional-episodes 1` exit 0 (attempt 6, seed 5); `run --controller q_learning --checkpoint milestones/episode_005.json` on seed 101 exit 0, 623 learned choices and 3 queue fallbacks (`incomplete_action_coverage`). Per-episode update counts match the chunk 4 probe exactly (deterministic).
- Resume equivalence: training 2 episodes straight and training 1 then resuming 1 give identical tables, epsilon, and RNG state.
- Attempt index on resume: first implemented by counting run folders and wrongly described as a gap in the design doc. Section B already specifies it; corrected to read each run manifest's `episode_attempt_index` and stop with a recovery error if any manifest is missing or corrupt.
- Changes to earlier code: training run IDs are now `train-attemptNNN-seedSSS` (attempt index, not episode number); `train()` returns a `TrainingResult`; `EventWriter` remembers a failed write or flush and never writes again; a regression where a failed final log write still reported `persistence_ok` was caught by the chunk 3 test and fixed.
- Not separately tested: a flush-only failure (the log-failure test fails the write first; flush shares the path); the directory-fsync durability text.

### 6. Full explanation layer
- Files: `explain.py` (full), `report.py` (full), `cli.py` (`summarize`), `summary.json`/`metrics.csv`
- Approved choices: Q-values shown as "estimated discounted future congestion cost" (Section G wording; Module 7 aligned); completed vehicles and remaining demand reported as not recorded until chunk 7; `metrics.csv` is one row per run; fixed-time decisions state their switch threshold as the contrast. Built in two internal steps: (a) replay, counterfactuals, fidelity, wording; (b) policy grids, report, summary.
- Tests: historical policy-revision replay; counterfactual search order; ambiguous-bin labels; saturation tie wording; untrained vs zero; report links match event IDs; no Q grid for baselines

- Result: 298 unit + 115 integration tests pass in the container. On real runs of fixed-time, queue, Q-learning training, and frozen Q-learning (H=600), every decision's explanation passed the fidelity re-check; counterfactuals were recomputed against an independent brute-force search (correct action, no closer differing state); the frozen run's policy grid matched the checkpoint cell by cell; report links all resolve; summary metrics match an independent calculation.
- Report checked visually in Chrome on a demo frozen run (`outputs/chunk6_demo`, not committed). Fixed from that review: summary overflowed sideways; the W/E-preferred and fallback cell colors were nearly identical (misleading); grids stacked vertically.
- Selection rules are now shared pure functions (`queue_choice`, `greedy_choice`) used by both the controllers and the fidelity check.
- Observation, not a result: in the small frozen tables tried, every fully trained state with one green preferred the same action, so frozen-run counterfactuals were all "none" (correct; brute force agrees). Early training also disagreed with the queue rule often (76 of 81 greedy choices in the demo). Both reflect very short training, not the final experiment.
- Not in chunk 6: vehicle accounting (summary says "not_recorded"), robustness truth-versus-measurement wording, experiment-level aggregates (chunk 7).

### 7. Experiments and results
- Files: `experiments.py`, `faults.py` (noise wrapper), `cli.py` (`tune-fixed`, `evaluate`), vehicle accounting
- Approved choices: masked demand logged once per second it holds (summary also counts separate stretches); phantom demand once per forced switch; results write-up in `docs/results.md` (committed, every number cites its run or experiment ID); each lane draws exactly two uniforms per second (stale test, then noise = floor(u * (2b + 1)) - b), never `randint`; final and robustness suites use the episode-15 milestone.
- Tests: identical draw sequences across fault levels; phantom/masked detection on injected cases; tuning never reads final seeds; every vehicle classified exactly once

- Result 7a: 323 unit + 126 integration tests pass in the container. Reduced-suite integration run (H=300, development seeds 100-101, final seeds 200-201, candidates 20/30 s): tuning, 2-episode training, final comparison and robustness suites all complete; every vehicle classified exactly once in every final run; every generated explanation passed the fidelity sweep (0 mismatches, 0 unavailable); clean runs report zero phantom/masked demand.
- Phantom/masked demand did not occur in the short reduced runs; detection is verified on injected known cases (unit) and the explanation wording on synthetic logs. Whether they occur at full horizon is a 7b result.
- 7b plan: `tune-fixed` (9 candidates x 3 development seeds), `train` 15 episodes with milestones 5/10/15 and development evaluations, `evaluate --suite final` and `--suite robustness` with `milestones/episode_015.json`, then `docs/results.md`.

- Result 7b: full configured suite run; write-up in `docs/results.md`. Tuning chose 15 s (edge of the grid). Training 15/15 episodes (7,178 updates; epsilon floor reached). Final comparison (mean queued vehicles over seeds 200-204): queue rule 5.12, fixed 9.59, Q-learning 11.46; Q-learning worse than the queue rule on all 5 seeds and left 212 vehicles uninserted on the peak seed. Development curve not monotonic (episode 10 better than 15; episode 15 used as specified). Robustness: low noise null for both; high noise +0.35 for the queue rule, -0.72 for Q-learning; zero phantom/masked demand (genuine null: every forced switch had at least 17 true competing vehicles). Fidelity sweep: 23,487 explanations, 0 mismatches, 0 unavailable. Two counting errors in the first draft of the write-up were caught and corrected before review.

## Decisions

- ~~Develop natively on macOS.~~ Superseded.
- SUMO runs in a Linux container (`python:3.11.17-slim`, linux/aarch64 on Apple Silicon) via `./dev.sh`. Reason: the macOS 1.24.0 wheel links against Homebrew library versions (Arrow 18, GDAL 3.10, ...) that Homebrew no longer ships. The container adds 5 apt libraries (`libx11-6 libxext6 libxrender1 libgl1 libexpat1`) that the manylinux binaries load even headless. Still SUMO 1.24.0; arm64 rather than the doc's x86-64 reference.
- Tae makes all commits and pushes. GitHub repo: https://github.com/taekim0508/traffic-cps (private).
- Repository root is `xcps/` (the doc's tree shows `traffic-controller/`).

## Open questions

- None open. Resolved items are recorded in the design doc, Section K.

## Environment notes

- Run everything through `./dev.sh` (needs Docker Desktop running). `./dev.sh build` after dependency changes.
- Host `/opt/homebrew/bin/sumo` and the host `.venv` SUMO binaries are broken (missing native libraries); `doctor` reports this. Do not use them.

## Log

- Chunk plan agreed. Native macOS chosen.
- Repo created and initial docs pushed. Native macOS failed (library versions); switched to Linux container. Chunk 0 implemented and tests passing; awaiting review.
- Chunk 0 approved (network content hash, config rules). Chunk 1 plan approved; started.
- Chunk 1 implemented; all tests pass; awaiting review.
- Chunk 1 approved (committed by Tae). Chunk 2 plan approved; started.
- Chunk 2 implemented; all tests pass; saturation check passed; awaiting review.
- Chunk 2 approved (committed by Tae, 8d5d757). Baseline before chunk 3: 201 tests pass in the container. Chunk 3 planning started.
- Chunk 3 plan approved; reward-per-second test moved to chunk 4; started.
- Chunk 3 implemented; all tests pass; awaiting review.
- Chunk 3 approved (committed by Tae, fa3dc63). Chunk 4 planning started.
- Chunk 4 plan approved (Q1 and Q2 option A); started.
- Chunk 4 implemented; all tests pass; awaiting review.
- Chunk 4 approved (committed by Tae, a477c0e). Chunk 5 planning started.
- Chunk 5 plan approved (all recommended options); started.
- Chunk 5 implemented; all tests pass; awaiting review.
- Attempt-index resume corrected to Section B (manifests, recovery error); 367 tests pass.
- Chunk 5 approved (committed by Tae, 0a3e37e). Chunk 6 planning started.
- Chunk 6 plan approved (all recommended options); started.
- Chunk 6 implemented; all tests pass; report reviewed visually; awaiting review.
- Chunk 6 approved (committed by Tae, 94b0a6b). Chunk 7 planning started.
- Chunk 7 plan approved (all recommended options); 7a started.
- 7a implemented; all tests pass; awaiting review before 7b runs.
- 7b approved and run; results in docs/results.md; chunk 7 awaiting review.
- Final comprehensive check: 449 tests pass; true clean-clone run (scenarios deleted, own source) builds network and demand byte-identically and runs every controller, train/resume, explain/summarize/report; no XCPS references; two stale code comments fixed. README rewritten for the finished project. `scenarios/smoke` regenerated so its manifest records the peak calibration (demand, network copy, and demand manifest byte-identical; only the calibration record was added).
