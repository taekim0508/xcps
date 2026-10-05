# Results

Results of the configured experiment suite (design doc Module 8). Every number below comes from a generated artifact under `outputs/` (not committed) and names the experiment or run it came from. These are simulation results for one synthetic intersection and five held-out traffic scenarios. They are not a claim about real roads, statistical significance, or human understanding.

## Setup

| Item | Value |
|---|---|
| Toolchain | SUMO, TraCI, sumolib 1.24.0; Python 3.11.17 (Linux container, aarch64) |
| Configuration | `configs/default.yaml`, resolved config hash `1b3420c99cf9...` |
| Horizon | 3,600 simulated seconds per run, one-second observations, five-second policy checks |
| Timing | 2 s yellow, 5 s minimum green, 60 s green-service limit |
| Seeds | training 0 to 14, development 100 to 102, final 200 to 204 (profiles cycle balanced, asymmetric, peak) |
| Experiments | `outputs/fixed_tuning_001`, `outputs/training_001`, `outputs/final_001`, `outputs/robustness_001` |

## 1. Fixed-time tuning (development seeds only)

Source: `outputs/fixed_tuning_001/fixed_tuning.json`. Grid search on mean queued vehicles (true counts) averaged over development seeds 100 to 102; ties go to the shorter target. This is tuning on the project's own metric, not a Webster-formula derivation.

| Target (s) | 15 | 20 | 25 | 30 | 35 | 40 | 45 | 50 | 55 |
|---|---|---|---|---|---|---|---|---|---|
| Mean queued vehicles | **9.061** | 9.280 | 10.325 | 10.705 | 10.548 | 10.319 | 11.127 | 11.901 | 12.592 |

Chosen target: **15 s**. All 27 runs completed. The realized green is 18 s (the first eligible five-second check after the target), so the cycle is 40 s and every final fixed-time run made 180 switches, all attributed to `fixed_schedule`, with zero forced switches.

**Limitation:** the best value is at the edge of the configured grid (the shortest candidate). A target below 15 s might do better; the candidate list was not extended, because changing it after seeing results would be a recorded design change, not part of this run.

## 2. Training

Source: `outputs/training_001` (15 attempts, all completed; final checkpoint `training_001-g00078`, evaluated copy `milestones/episode_015.json`).

- 7,185 accepted choices, 7,178 completed updates, 7 unfinished intervals discarded at horizon cutoffs.
- Exploration reached its floor (epsilon 0.1) during training, as expected after 4,605 accepted choices.
- The episode-15 table has 27 states; 26 have both actions updated. States reached queue bins 0 to 4 on one side but never two high bins at once (seen pairs: (0,0) to (0,4), (1,0) to (1,2), (2,0), (2,1), (3,0), (4,0)).

Development evaluations of the immutable milestones (mean queued vehicles, true counts, seeds 100 / 101 / 102):

| Milestone | Seed 100 | Seed 101 | Seed 102 | Mean |
|---|---|---|---|---|
| Episode 5 | 25.47 | 22.62 | 22.74 | 23.61 |
| Episode 10 | 2.66 | 7.74 | 8.58 | 6.33 |
| Episode 15 | 7.81 | 8.70 | 26.74 | 14.42 |
| (Fixed 15 s, same seeds) | 3.60 | 15.82 | 7.76 | 9.06 |

Learning is not monotonic: the episode-10 policy did much better on the development seeds than the episode-15 policy, mainly on the peak seed (102). Per the design, the final comparison uses episode 15 as specified in advance; the episode-10 result is not substituted after the fact, because that would be selecting a model on a test-like comparison.

## 3. Final comparison (seeds 200 to 204)

Source: `outputs/final_001/experiment.json` and `runs.csv`. All 15 runs completed. Each seed's demand file was generated once and reused by all three policies (identical scenario hashes per seed). Frozen Q-learning used the table for almost every choice: fallback share was 0.0 on four seeds and 0.002 on seed 202, so this comparison measures the learned policy, not the queue rule.

Mean queued vehicles (true counts; lower is better):

| Seed (profile) | Fixed (15 s) | Queue rule | Q-learning (ep. 15) |
|---|---|---|---|
| 200 (balanced) | 3.737 | **3.533** | 7.925 |
| 201 (asymmetric) | 16.865 | **5.479** | 8.296 |
| 202 (peak) | **6.999** | 8.686 | 26.459 |
| 203 (balanced) | 3.766 | **2.460** | 7.043 |
| 204 (asymmetric) | 16.564 | **5.451** | 7.561 |
| Mean (sample SD) | 9.586 (6.642) | **5.122 (2.374)** | 11.457 (8.399) |

Paired differences in mean queued vehicles (row minus baseline, five seeds):

| Comparison | Mean difference (SD) | Relative change of means | Seeds where the row policy was lower |
|---|---|---|---|
| Queue rule vs fixed | -4.464 (6.286) | -46.6% | 4 of 5 |
| Q-learning vs queue rule | +6.335 (6.479) | +123.7% | 0 of 5 |
| Q-learning vs fixed | +1.870 (11.661) | +19.5% | 2 of 5 (the asymmetric seeds) |

Peak queued vehicles (means): fixed 22.6, queue rule 17.2, Q-learning 36.8.

**Result: the simplest adaptive rule (the queue rule) was best overall, and the learned Q-learning policy was worst.** Q-learning beat fixed-time only on the two asymmetric seeds, where fixed-time's equal split cannot serve the heavy direction.

Unmet demand matters for reading these numbers (vehicle accounting, `vehicle_accounting` in each run's `run_end`). A vehicle that cannot enter the road is not counted in the queue, so a low queue can hide it:

- Fixed-time left 148 (seed 201) and 145 (seed 204) scheduled vehicles never inserted, so its asymmetric-seed queues understate its real congestion.
- Q-learning left 212 vehicles never inserted on the peak seed (202), on top of the highest queue.
- The queue rule inserted every scheduled vehicle on every seed.

Every scheduled vehicle was classified exactly once (arrived, still active, or never inserted) in every final run.

### Why Q-learning did worse (from the logs and the global policy view)

- **It rarely switches.** Per hour, Q-learning made 69 to 112 accepted switches; the queue rule made 283 to 343.
- **It reaches the 60 s service limit.** Q-learning runs had 10, 0, 26, 4, and 2 forced switches (seeds 200 to 204); the queue rule and fixed-time had none. Those 0 to 26 events per run are explained as operating-rule actions, not controller choices.
- **It disagrees with the queue rule on 75.6% of its choices** (2,331 of 3,084 free choices), mostly by keeping the current green when the other direction's queue score is higher.
- **Coverage is narrow.** The table never saw both directions loaded at once (see Training), so the grid gives no learned guidance for heavy two-way congestion, which is exactly the peak period where it failed worst.

Likely contributors, stated as hypotheses rather than findings: tabular states with coarse queue bins cannot distinguish "competing queue is growing" from "competing queue is stable"; the duration-aware reward with no explicit switching cost makes the 7 s of yellow plus minimum green after a switch look expensive; and 15 one-hour episodes may be too few. None of these was tested here.

## 4. Sensing robustness (seeds 200 to 204)

Source: `outputs/robustness_001/experiment.json`. Queue rule and frozen Q-learning, each clean, low (±1 vehicle, 5% stale), and high (±3 vehicles, 20% stale). Both fault levels used the same noise seed per scenario (offset 10,000 plus the traffic seed). All 30 runs completed. Metrics use true counts; the controllers saw measured counts.

| Policy | Clean mean | Low mean (paired diff) | High mean (paired diff) |
|---|---|---|---|
| Queue rule | 5.122 | 5.174 (+0.052, SD 0.241) | 5.471 (+0.349, SD 0.329) |
| Q-learning | 11.457 | 11.466 (+0.009, SD 0.735) | 10.737 (-0.720, SD 0.755) |

Mean absolute lane-count error was about 0.5 vehicles at low and about 1.3 at high.

- **Low noise: a null result** for both policies (mean change under 0.06 vehicles, smaller than the seed-to-seed spread), as the design predicted.
- **High noise:** the queue rule got slightly worse (+0.35 vehicles, +6.8%; worse on 4 of 5 seeds). Q-learning's mean got slightly better (-0.72, -6.3%). That is not evidence that noise helps; a plausible reading is that noise occasionally knocks the policy out of its long holds, but this was not tested.
- **Phantom and masked demand: zero events in all 20 noisy runs.** This is a genuine null, not a detection failure. Detection is verified on injected cases (unit tests). In these runs, every one of the 66 forced switches happened with at least 17 vehicles truly waiting on the competing side, and no green ever reached 60 s with 3 or fewer vehicles truly waiting on the other side, so ±3 noise could neither invent nor hide the demand that triggers the rule. Lighter competing demand at the service limit would be needed to exercise these cases.

## 5. Explanation fidelity

Across the 45 final and robustness runs, 23,487 decisions and events were explained. 23,337 were verified by re-running the recorded selection rule on the recorded evidence (and, for Q-learning, on the decision-time table rebuilt from the run's policy start plus ordered updates). The remaining 150 were forced-switch (operating-rule) events, for which a controller-rule check does not apply. **0 mismatches, 0 unavailable explanations.** Every run has `report.html`, `summary.json`, and `metrics.csv`; the Q-learning reports include the global policy grid for the evaluated checkpoint.

This verifies that explanations match the recorded decision process. It does not show that people understand the controller better; no user study was run.

## Limitations

- Five final scenarios give initial variability evidence only; no significance tests were run, and the standard deviations are large relative to some differences.
- One synthetic two-lane intersection with straight-through traffic only; no turns, pedestrians, or real demand data.
- The fixed-time optimum sits at the edge of the tuning grid.
- The Q-learning result reflects this specific state encoding, reward, and 15-episode budget. The episode-10 milestone did better on development seeds; this report does not claim a better learned policy exists, only that the specified one underperformed.
- Sensor noise is an illustrative model, not a calibrated detector model.
