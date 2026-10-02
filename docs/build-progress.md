# Build Progress

Tracks the 8 development chunks and the current position. Chunks follow the design doc's "Implementation order" (steps 0 to 7) exactly. The design doc remains the specification; this file only records status and decisions.

**Current chunk:** 0 (awaiting review)

Status values: `not started`, `in progress`, `awaiting review`, `done`.

## Chunks

| # | Chunk | Status | Gate |
|---|---|---|---|
| 0 | Foundation: scaffold, environment, scenario | awaiting review | `doctor` passes on 1.24.0; `scenario build` produces a validated network |
| 1 | Simulator interface and safety | not started | Real SUMO steps under the monitor with zero violations; fault-injection cases pass |
| 2 | Thin vertical slice | not started | `run` -> `explain` -> `report.html` on one short run; peak saturation check recorded |
| 3 | Fixed-time and full runtime enforcement | not started | Full-horizon fixed and queue runs with correct attribution |
| 4 | Tabular Q-learning | not started | Short training learns; frozen evaluation leaves the table unchanged |
| 5 | Persistence and failure-safe shutdown | not started | Train -> interrupt -> resume -> evaluate cycle works |
| 6 | Full explanation layer | not started | Fidelity checks pass on all three controllers |
| 7 | Experiments and results | not started | 7a: code and tests pass. 7b: tuning, training, comparison, robustness runs, honest results summary |

## Chunk details

### 0. Foundation: scaffold, environment, scenario
- Files: `pyproject.toml`, `README.md` (stub), `configs/default.yaml`, `src/traffic_cps/{__init__,__main__,cli,config,contracts,scenario}.py`, `requirements-lock.txt`
- Also: `Dockerfile`, `dev.sh`, `.dockerignore` (container runtime, see Decisions)
- Tests: every Section C config rejection rule; byte-identical demand XML for a fixed seed; profile cycling by seed; network has exactly 4 movements and positive generated lane lengths (integration)
- Result (2026-10-02): 79 unit + 7 integration tests pass in the container; `doctor` passes; `scenarios/single` and `scenarios/smoke` generated. Generated incoming lane lengths: N/S 246.00 m, W/E 239.60 m.
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
- Tests: masks derived from `getControlledLinks`; exact one-second advance; startup exception; vetoes for direct opposing green, premature yellow, late forced switch; same-time read retries (3 attempts max) then explicit failure; queue score and capacity arithmetic

### 2. Thin vertical slice
- Files: `controllers/__init__.py`, `controllers/queue.py`, `actuator.py`, `runtime.py` (minimal), `events.py` (minimal), `explain.py` (minimal), `report.py` (timeline), `cli.py` (`run`, `explain`, `report`)
- Tests: queue rule with `1e-9` tolerance and current-green tie wins; actuator accepted/blocked; 10s -> 12s -> 20s timeline; 60s smoke run end to end
- Then: peak saturation check; regenerate demand and record in manifest if > 25%

### 3. Fixed-time and full runtime enforcement
- Files: `controllers/fixed.py`, `runtime.py` (full Section E ordering), `cli.py`
- Tests: `fixed_schedule` attribution; target >= service limit rejected; service-limit operating action plus S4; 3,601 observations; no command at or after H; reward once per second

### 4. Tabular Q-learning
- Files: `learning.py`, `controllers/q_learning.py`, `runtime.py` (train/evaluate), `cli.py` (`train`, basic)
- Tests: bin edges; hand-computed Q-update; counts only on completed updates; epsilon progress; coverage fallback; feedback discard at cutoff/failure; frozen evaluation never updates

### 5. Persistence and failure-safe shutdown
- Files: `checkpoints.py`, `events.py` (all event types, manifests, `policy_start.json`), `runtime.py` (shutdown), `cli.py` (`--resume`, milestones, seed allocation)
- Tests: zero entries, RNG, counters round-trip; missing/corrupt/incompatible rejection; latest/previous rotation; interrupted write; log and checkpoint write failures give correct exit codes

### 6. Full explanation layer
- Files: `explain.py` (full), `report.py` (full), `cli.py` (`summarize`), `summary.json`/`metrics.csv`
- Tests: historical policy-revision replay; counterfactual search order; ambiguous-bin labels; saturation tie wording; untrained vs zero; report links match event IDs; no Q grid for baselines

### 7. Experiments and results
- Files: `experiments.py`, `faults.py` (noise wrapper), `cli.py` (`tune-fixed`, `evaluate`), vehicle accounting
- Tests: identical draw sequences across fault levels; phantom/masked detection on injected cases; tuning never reads final seeds; every vehicle classified exactly once

## Decisions

- 2026-10-01: ~~Develop natively on macOS.~~ Superseded 2026-10-02.
- 2026-10-02: SUMO runs in a Linux container (`python:3.11.17-slim`, linux/aarch64 on Apple Silicon) via `./dev.sh`. Reason: the macOS 1.24.0 wheel links against Homebrew library versions (Arrow 18, GDAL 3.10, ...) that Homebrew no longer ships. The container adds 5 apt libraries (`libx11-6 libxext6 libxrender1 libgl1 libexpat1`) that the manylinux binaries load even headless. Still SUMO 1.24.0; arm64 rather than the doc's x86-64 reference.
- 2026-10-02: Tae makes all commits and pushes. GitHub repo: https://github.com/taekim0508/traffic-cps (private).
- 2026-10-01: Repository root is `xcps/` (the doc's tree shows `traffic-controller/`).

## Open questions

- Chunk 0 (needs Tae's approval, then record in design doc): netconvert stamps a timestamp into `network.net.xml`, so its raw hash changes on every rebuild. Proposal: the network manifest keeps the raw `sha256` (proves a copy is byte-identical) and adds `content_sha256` (header comment stripped) as the network identity for the Section F compatibility contract.
- Chunk 0 (needs Tae's approval): config interpretations of "edges inconsistent with the two-lane state encoding": queue edges must start at 0 and end at 2.0; elapsed edges must start at 0, have their second edge equal to `minimum_green_s`, and end at `green_service_limit_s`. Stricter-than-spec additions: `keep_previous` must be 1, `flush_each_tick` must be true, development and final seeds must not overlap, duplicate YAML keys are rejected.

- Chunk 3: Section J says "720 scheduled checks including skipped time zero" (t = 0..3595), but Section E step 4 treats t = H = 3600 as a boundary where feedback may close. Confirm whether H is a 721st command-free boundary, then record the answer in the design doc.

## Environment notes

- Run everything through `./dev.sh` (needs Docker Desktop running). `./dev.sh build` after dependency changes.
- Host `/opt/homebrew/bin/sumo` and the host `.venv` SUMO binaries are broken (missing native libraries); `doctor` reports this. Do not use them.

## Log

- 2026-10-01: Chunk plan agreed. Native macOS chosen.
- 2026-10-02: Repo created and initial docs pushed. Native macOS failed (library versions); switched to Linux container. Chunk 0 implemented and tests passing; awaiting review.
