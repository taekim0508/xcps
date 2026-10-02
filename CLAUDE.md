# Explainable Traffic Signal Controller (traffic-cps)

## What this project is

A clean-room, standalone simulated cyber-physical system: one signalized intersection generated in SUMO and controlled through direct TraCI by a fixed-time, queue-based, or tabular Q-learning controller. Every observation, decision, command, signal change, safety check, and learning update is recorded and explained to a human from linked evidence. It is a new-grad SWE portfolio project. Goals, in priority order:

1. **Faithful explanations people can see.** Every explanation matches the branch and values that actually produced the decision, and is viewable without reading raw logs (`explain`/`summarize` CLI, static HTML run report, global policy view).
2. **A real CPS loop.** Sense, decide, check safety, actuate, observe, once per simulated second, with timing rules, an independent safety monitor, and honest failure handling.
3. **Honest evaluation.** Matched-demand comparison of frozen Q-learning against a tuned fixed-time baseline and the queue-based rule, plus a two-level sensor-fault experiment. A result that favors the simpler controller is still a valid result.

This is a new implementation of the idea, not a reproduction of any earlier project. Do not cite or compare against historical results.

## Source of truth

- `docs/traffic-explainability-design.md` is the specification. **Start with its "Standalone build contract" section** (files, dependencies, config, interfaces, runtime ordering, schemas, CLI, acceptance). Modules 1 to 8 are the behavioral requirements. Where they differ, the build contract wins.
- `docs/interview-design-qa.md` is optional rationale. Read the relevant answer before changing a behavior it explains.
- If code, this file, and the design doc disagree, the design doc wins. If the doc is ambiguous or contradicts itself, stop and ask Tae; do not pick an interpretation silently. Record the resolution in the doc.
- Do not change a design decision on your own. Propose the change, explain why, and wait for agreement.
- Read only the sections a step needs. The doc is long; do not load it whole every session.

## Environment (pinned by the build contract, Section A)

- Python 3.11 (`requires-python >=3.11,<3.12`), Linux x86-64 reference platform.
- `eclipse-sumo==1.24.0`, `traci==1.24.0`, `sumolib==1.24.0`, `PyYAML==6.0.2`; dev: `pytest==8.3.5`.
- No SUMO-RL, Gymnasium, ML framework, LLM, CDN, or legacy XCPS code.
- The 1.24.0 SUMO wheel targets macOS 15+ ARM64 only. On any other Mac, use Linux (VM or container) rather than switching SUMO versions. A version change is a deliberate, recorded compatibility change.
- `traffic-cps doctor` must pass before any integration test.

## Layout

Follow the build contract's tree exactly: `src/traffic_cps/` (cli, config, contracts, scenario, adapter, sensor, safety, actuator, controllers/, learning, runtime, events, checkpoints, explain, report, experiments, faults), `configs/default.yaml`, `tests/{unit,integration,fixtures}/`, generated `scenarios/` and `outputs/` (outputs not committed). Do not add corridor/network supervisors, L2/L3 scaffolding, an LLM explanation path, or a live dashboard.

## Build order and gates

Follow the design doc's "Implementation order" exactly. Do not start a step until the previous step's acceptance checks pass and Tae has reviewed them.

0. Scaffold the project, install pinned deps, `doctor`, scenario generation, network smoke checks.
1. Adapter one-second stepping, mask mapping, timers, sensor snapshots, safety monitor with fault injection.
2. Thin vertical slice: queue policy -> actuator -> event log -> explanation -> timeline report on a short run. Then run the peak saturation check.
3. Fixed-time policy (target below the service limit) and full runtime enforcement.
4. Tabular Q-learning training and frozen evaluation.
5. Checkpoints, restore, failure-safe shutdown.
6. Full explanation layer (templates, counterfactuals, global policy view, summaries, fidelity checks).
7. Tune fixed-time on development seeds, train, final comparison, two-level robustness, results summary.

For each step: state the plan and the exact files you will create or edit, get agreement, write tests from the relevant acceptance checks first, implement, run the tests, then report what passed, what failed, and what was not tested.

## Non-negotiable engineering rules

- **No silent defaults.** Never substitute zero, an old reading, a default mask, a remapped action, or an empty Q-table for missing or invalid data. Fail explicitly per the doc's failure policy.
- **One writer, one clock.** Only the adapter writes signal state, every write passes the safety monitor and is read back, and only the runtime advances time, one second at a time. No SUMO signal program or second scheduler runs in parallel.
- **Submission is not execution.** Only an observed signal change confirms a command took effect.
- **Decision-time evidence is immutable.** Explanations use the policy revision at decision time (reconstructed from `policy_start.json` plus ordered updates), never later values.
- **Separate sources:** controller decision, fixed schedule, operating-rule action (min green, service limit, transition), and safety-monitor intervention are always distinct in records and wording. Exploration is explained as exploration even when it matches the greedy action.
- **Say when a tie comes from saturation.** If both scores are at their cap, the explanation says so.
- **Truth versus measurement.** In robustness runs, policy and reward use measured counts; metrics use true counts; explanations show both.
- Unrounded values internally; round only display text. Tie tolerance is absolute `1e-9`; current green wins ties when rules permit.
- Strict JSON (no NaN), never pickle or eval records, never overwrite an existing run directory.

## Testing rules

- Every acceptance check in the doc (module checks and build-contract Section J) becomes at least one automated test, named after the module and check.
- Unit tests must run without SUMO and use small deterministic cases with hand-computed expected values (bin edges, Q-update arithmetic, the 10s -> 12s -> 20s timeline).
- Integration tests use real SUMO for short horizons. Do not mock SUMO in tests that claim to verify transitions or timing.
- Fault injection is required for: sensor read failure, command failure, log write failure, checkpoint write failure, corrupt/incompatible checkpoint load, safety violations, and phantom/masked demand.
- Never weaken, skip, or delete a failing test to make a step pass. Report it.

## Honesty in claims

- Do not say something works until a test or run shows it. Use design tense for anything unverified.
- Never tune on final seeds. Fixed-time tuning uses development seeds only and is recorded.
- Report failed or incomplete runs separately; never fold them into aggregates. Report null results (for example, no degradation under low noise).
- Claim explanation fidelity and traceability only; do not claim improved human understanding without a user study.

## Working with Tae

- Before multi-step work, show the plan and the exact files you will create or edit, and wait for approval.
- Explain design and implementation choices well enough for Tae to learn them, including the reasoning and the alternatives rejected.
- Be blunt about problems, risks, and weak results. No filler, no em-dashes.
- Keep token use reasonable: read the specific sections you need.
