# Explainable Traffic Controller: Standalone Implementation Design

**Purpose:** Build a traffic simulation whose observations, decisions, and behavior can be inspected and explained. The project emphasizes understandable components, explicit interfaces, failure handling, and testing.

**Implementation entry point:** Read the **Standalone build contract** at the end first for files, dependencies, commands, data contracts, and runtime ordering. Then use Modules 1–8 as the behavioral requirements. All referenced project paths name files to create in the new repository, not files assumed to exist elsewhere.

## Implementation status and required scope

This is a specification for a new application, not a report of completed software. Implement and test the requirements below from scratch. No existing XCPS code, SUMO network, trained table, checkpoint, or interview Q&A is required. Start new training with an empty table; create checkpoints through that training before testing resume or frozen evaluation.

- **Required application:** one intersection, generated network and demand, sensor, three selectable controllers, actuator, adapter, independent safety monitor, runtime, event logging, and training checkpoint recovery.
- **Required explainability:** linked decision/execution/learning evidence, readable explanations, policy grids, supported contrastive queries, and an offline HTML report.
- **Required validation:** component and integration checks, training and matched controller comparisons, fixed-time tuning on development scenarios, and the specified sensor-noise experiments. These experiments validate the finished application; they are not prerequisites for building its first working slice.
- **Outside this implementation:** reward-decomposition tables, a human-participant study, a second lay-audience interface, a live dashboard, multiple intersections, additional turning movements, DQN, all-red clearance, and deployment to physical signals. Do not add these automatically after completing the required work.

Read the Standalone build contract first, follow the implementation order, and use each module's acceptance checks to verify the result. Historical planning labels are not deferred requirements. Examples illustrate formats and behavior; they are not existing artifacts or measured results.

## Scope and end-to-end pipeline

Build the components required for one intersection: SUMO adapter, sensor, selectable controller, actuator, runtime, event/checkpoint storage, explanation formatter, and experiment runner, plus an independent safety monitor (Module 4A). Retain the sensor → decision → actuation hierarchy.

The runner loads configuration and optionally a checkpoint, initializes SUMO, and starts a fresh episode. The runtime advances one simulated second, senses traffic, enforces operating rules through the adapter, and records events. At eligible five-second control points it closes pending feedback, updates learning in training mode, selects a new action, and submits it through the actuator. The adapter confirms actual signal behavior. The explanation formatter reads linked evidence, while periodic checkpoints preserve completed training. At the horizon, the runtime closes the run using the agreed cutoff rule.

Implement all components from this specification. Use Python and direct SUMO TraCI. The adapter owns one-second advancement and all signal transitions; no second scheduler may advance time or cycle phases. The standalone build contract defines environment setup, newly generated network/demand assets, configuration, module interfaces, file schemas, CLI, and runtime ordering.

### Baseline scope

The baselines are the fixed-time and queue-based controllers implemented in this new application. There is no requirement to obtain or run an original prototype. Historical waiting-time reductions are not acceptance criteria or results of this build. Do not claim reproduction of unavailable experiments.

### CPS framing

The project is a **simulated closed-loop cyber-physical system**: SUMO stands in for the physical plant (vehicles and the signalized intersection); the sensor, controller, actuator, and safety monitor are the cyber side; the loop closes once per simulated second. State these limits wherever the project is presented:

- Time is simulated, discrete, and one-second-resolution. There are no wall-clock deadlines or real-time scheduling claims.
- Sensing is idealized by default: the sensor reads simulator ground truth (halting-vehicle counts), which physical loop detectors cannot measure directly. Module 1 records this, and Module 8 adds a noisy-sensing experiment.
- There is no all-red clearance interval in this simplified version. The design makes no claim that its timings or safety monitor establish road-deployment suitability.
- The safety property that defines a signal controller (conflicting directions are never both permitted to enter) is checked by an independent monitor, not only by the code that schedules phases (Module 4A).

## Module 1: Sensor

### Responsibility

Read the current simulated traffic situation and return one consistent observation. Provide both understandable vehicle counts and the congestion scores needed by the controllers. The sensor reports traffic; choosing a signal belongs to the controller.

Create `TrafficSensor` in `src/traffic_cps/sensor.py`: read validated per-lane counts and actual signal state through the adapter; compute the observation contract below. Do not depend on an existing sensor class.

### Observation contents

| Information | Definition |
|---|---|
| Simulation time | Seconds elapsed in the simulation when this observation was captured. |
| Waiting vehicles per lane | A nonnegative integer count of vehicles classified as queued by the simulator, keyed by incoming lane ID and direction group. |
| Waiting vehicles per direction | Sum of the corresponding lane counts, shown as north–south and west–east totals. |
| Queue scores | Sum of the two normalized lane queue values for each direction, using the explicit formula below. |
| Current light color | Actual red, yellow, or green for each incoming lane's controlled movement. There is no single color for the entire intersection. |
| Time at current color | Seconds since that movement last changed color. A transition from green to yellow resets this duration. |

The network to generate contains north-to-south and west-to-east straight-through flows, with two incoming lanes each. N/S and W/E are direction-group labels, not a claim that all four approaches are present. Map actual lane IDs and controlled movements explicitly rather than assuming that the first half of an arbitrary lane list is N/S.

### Counts and scores

Counts answer “How many vehicles are waiting?” Scores answer “How large is the queue relative to lane capacity?” A score is not simply the number of vehicles divided by the number of lanes.

Define normalization here instead of inheriting a library implementation. All vehicles use length 5m and minimum gap 2.5m. For each incoming lane, read its actual generated length `L` from TraCI once at startup, require `L > 0`, and set estimated capacity `C = L / 7.5` (a positive float, not rounded). At each observation, let `n` be that lane's integer halting count and calculate `lane_score = min(1.0, n / C)`. Sum the two scores in each direction. Direction scores lie in [0, 2]. Validate counts before normalization; a corrupt input is not made valid by clipping.

Read counts with `lane.getLastStepHaltingNumber`: SUMO defines halting as speed below 0.1m/s. Source: [TraCI lane API](https://sumo.dlr.de/pydoc/traci/_lane.html). Preserve the original counts rather than reconstructing them from scores. Capacity is a project-defined estimate, not the maximum number of cars guaranteed to fit. Reward and scores saturate at estimated lane capacity. Raw counts expose extra vehicles on measured lanes; neither counts nor scores observe upstream vehicles that have not entered the network, which must be reported as uninserted demand.

**Sensing realism:** Use simulator ground truth by default and name the component `TrafficSensor`, not a physical loop detector. No E2-detector implementation is required in this version. The separately labeled evaluation noise wrapper changes measured counts while retaining ground truth for metrics; it does not silently substitute stale data when a real required read fails. Its exact parameters are defined in the standalone build contract.

### Timing and integration contract

Retrieve all required values without advancing simulation time. A snapshot is delivered to the controller only when all required fields are valid.

Actual light color and the controller's logical green direction are different concepts, especially during yellow transitions. The observation carries `phase`, nullable `current_green`, and nullable `transition_target` as defined in the build contract (Section D); controllers never infer direction from a raw mask or integer.

The simulation adapter must supply accurate color-change times, including changes between controller observations. Counting only five-second controller samples could miss a shorter yellow phase. Color durations come from observed color onsets, not from a separate phase timer. Module 4 and the build contract (Section E) define the adapter behavior.

### Retrieval failure policy

1. Make the initial retrieval attempt.
2. If a required measurement is unavailable or invalid, record the failed field and error and notify the user that retrieval is being retried.
3. Retry at most twice without advancing the simulation or requesting a new control decision: three attempts total.
4. If a valid snapshot is recovered, record recovery and continue normally.
5. If failure persists, report it to the runtime. End the simulation cleanly, notify the user, and preserve completed records with an incomplete-run label.

All observation fields above are required in this design. Never substitute zero or an old reading for an unavailable current measurement. Failure to save an otherwise valid record is a separate issue for the logging module.

### Explainability contribution

Vehicle counts provide familiar context; scores expose the actual queue-based controller inputs; light colors and durations describe observed behavior. These observations provide evidence for explanations but do not themselves explain a controller's decision.

For example, an eventual display could show “W/E: 12 waiting vehicles; N/S: 4 waiting vehicles” alongside the scores and actual lights. These numbers are illustrative. Module 7 specifies explanations using the evidence actually used.

### Acceptance checks for implementation

- Direction totals equal the sum of their lane counts, and scores equal the sum of their normalized lane queue values.
- Every lane has the correct direction and controlled-light mapping.
- Counts, scores, colors, and durations share one simulation timestamp.
- Missing readings remain distinct from legitimate zeros.
- A transient failure recovers within two retries; persistent failure ends the run and labels partial results correctly.
- Color-duration tracking captures a yellow transition shorter than the control interval.

## Module 2A: Queue-based controller

### Responsibility and selection rule

Create `QueueController` in `src/traffic_cps/controllers/queue.py`. Use a valid observation to request a direction and record the selection evidence. Name it queue-based, not max-pressure: it compares incoming queue scores without subtracting downstream queues.

The preference rule is to request the direction with the higher queue score. If the scores are equal, retain the current green. Do not use a fixed direction order to break ties.

### Operating behavior

Evaluate these conditions in priority order:

| Priority | Condition | Behavior | Explanation reason |
|---|---|---|---|
| 1 | A yellow transition is underway | Complete the already scheduled transition without changing its target. | Transition in progress. |
| 2 | The current green has not reached its minimum duration | Retain the current green. | Minimum green time not reached. |
| 3 | The configured green-service limit has been reached and the other direction has waiting vehicles | Request service for the other direction. | Green-service limit reached with competing demand. |
| 4 | Neither preceding constraint applies and one queue score is higher | Request that direction; retain green if it is already active. | Higher queue score. |
| 5 | Neither preceding constraint applies and the queue scores are equal | Retain the current green, including when both queues are zero. | Equal queue scores; retained current green. |

The green-service limit takes priority over retaining green on equal scores when the other direction has waiting vehicles. It does not require an unnecessary switch when no vehicles are queued in the competing direction. A request to switch must pass through the configured yellow transition.

This is a simplified queue-based policy with timing constraints. It does not reproduce gap-based actuated control: queued-vehicle observations alone do not measure gaps between moving vehicles. Background references: [FHWA actuated timing principles](https://ops.fhwa.dot.gov/publications/signal_timing/04.htm) and [SUMO traffic-light control](https://sumo.dlr.de/docs/Simulation/Traffic_Lights.html).

### Signal timing defaults

Create these numeric defaults in the new `configs/default.yaml`:

| Setting | Default | Meaning |
|---|---|---|
| Yellow duration | 2 simulated seconds | The outgoing direction is yellow while the conflicting direction stays red, before the requested green begins. |
| Minimum green | 5 simulated seconds | Measure from the instant the requested direction actually becomes green, excluding yellow. No optional switch before this duration has elapsed. |
| Green-service limit | 60 simulated seconds | Measure from actual green onset. At or beyond this duration, competing queued demand requires a switch under the adapter's operating rules. |

These are configurable prototype defaults, not claimed optimal or universal road-signal settings. Implement a green → two-second yellow → opposite green sequence, with no additional all-red interval in this version.

The service timer runs from actual green onset, not from the arrival of competing demand. If no vehicles are queued in the competing direction at 60s, retain green. If competing demand subsequently appears, enforce the switch when observed without waiting for the next five-second policy check. Retaining green does not reset the timer. This is a simplified green-service rule, not a reproduction of every real-world maximum-green timer.

The minimum-green duration and policy-check interval are independently configured, even though both default to five seconds. The adapter's timing checks run with one-second simulation advancement. Exact elapsed time determines eligibility and forced switching, not the Q-table's coarse elapsed-time category.

These values set the four elapsed-green bins to `[0, 5)`, `[5, 32.5)`, `[32.5, 60)`, and `[60, infinity)` seconds. With integer-second observations, 32s is in the earlier window and 33s is in the later window. Persist these thresholds with the model configuration.

### Decision evidence

Record the observation reference, both unrounded queue scores, requested direction, and the actual reason that determined the request. Include the relevant elapsed time and configured limit for a timing-based reason. Vehicle counts provide readable context from the same observation; do not substitute them for the scores when explaining a score-based choice.

An equal-score reason must not be reported if the green-service limit actually triggered a switch. A transition-in-progress explanation describes continuation of an earlier decision, not a fresh queue-based selection. The eventual display must distinguish requested direction from observed light colors.

### Relationship to Q-learning

The queue comparison and equal-queue tie rule belong to this policy. Q-learning will select using learned action values or exploration, so equal queue scores alone will not determine its choice.

Minimum green and transition constraints, together with the agreed competing-demand service limit, are system operating rules. Module 2B honors them; exploration cannot bypass them. Modules 4 and 5 specify enforcement and feedback attribution.

### Acceptance checks for implementation

- A higher queue score determines the request when timing rules allow it.
- Equal scores retain current green regardless of phase ordering.
- Minimum green takes precedence over a competing higher queue score.
- A reached green-service limit with competing demand takes precedence over the queue preference or a tie.
- No switch is forced solely by the service limit when competing queued demand is absent.
- A transition completes without retargeting midway.
- Recorded reasons match the branch that actually determined the request.

**Integration defaults:** Initialize N/S green at time zero with elapsed green zero, and confirm the observation before proceeding. The time-zero policy check is skipped for minimum green. Compare queue scores or Q-values as tied when their absolute difference is at most `1e-9`, with no relative tolerance; use raw values for binning and record the tolerance. Apply ties only after operating constraints. Missing or invalid phase mappings stop initialization rather than defaulting silently. These choices do not imply road-signal timing compliance.

## Module 2B: Tabular Q-learning controller

This controller shares the sensor, actuator, adapter, operating constraints, and explanations with the queue-based policy.

### State and actions

Encode `(ns_queue_bin, we_queue_bin, current_green, elapsed_green_bin)`. Queue bins are `[0, 0.4)`, `[0.4, 0.8)`, `[0.8, 1.2)`, `[1.2, 1.6)`, and `[1.6, 2]` for each direction's two-lane summed normalized score. Reject invalid/nonfinite/out-of-range input rather than silently clipping. Elapsed bins are `[0, 5)`, `[5, 32.5)`, `[32.5, 60)`, and `[60, infinity)`. Current green is N/S or W/E. Yellow is recorded physical behavior, not a new selectable Q-state.

This encoding has 200 possible combinations and at most 400 state-action values. Not every combination is reachable or permits a choice, and binning is an approximation rather than a guarantee of a fully Markov state. Initialize encountered training entries to zero. Checkpoints using different bins or reward timing are incompatible.

**Reachability note:** Free choices occur only after minimum green, so elapsed bin 0 never appears as a decision state or a bootstrap state; it exists for completeness and display. In elapsed bin 3, a free choice exists only when the competing direction has no waiting vehicles (otherwise the switch is forced), so bin-3 decision states effectively have a competing queue bin of 0. Coverage reports should exclude unreachable combinations rather than presenting them as missing training.

Actions request N/S or W/E green. Requesting the current direction retains it without resetting its timer; requesting the other starts an accepted transition. Compare only actions permitted by the operating rules. Timing-determined actions bypass the policy and exploration schedule.

### Training and evaluation selection

- **Training:** Start epsilon at 1.0. With probability epsilon, choose uniformly among permitted actions; otherwise choose a highest-valued action, retaining current green on a tie. Record the actual branch and the epsilon and Q-values used before any later update. Decay epsilon after an accepted eligible request using `max(0.10, epsilon * 0.9995)`. Count these accepted choices across episodes; forced actions, skipped checks, and defensively blocked submissions do not advance this counter. The floor is reached after 4,605 counted choices, not necessarily after a fixed number of runs.
- **Evaluation:** Load a frozen table, disable exploration and learning, and do not insert unknown entries or change training counters/RNG state in the saved checkpoint. If the state is unseen or any currently permitted action lacks a completed training update, use the queue-based fallback and disclose `unseen_state` or `incomplete_action_coverage`. Otherwise exploit the Q-values with the current-green tie rule. This conservative extension resolves partially explored states without presenting initialized zeros as experienced estimates; one update still does not establish reliability.
- **Experience counts:** Per-action training visits used for coverage count completed, valid learning updates. State coverage is the sum of those counts. Keep request/selection counts separately, since an accepted choice can reach a cutoff before feedback completes.

### Learning contract

Remember the pre-action state and accepted request. Collect `r_i = -(ns_score_i + we_score_i)` once per subsequent simulated second. Close the interval at the next scheduled point permitting a fresh choice, or the eligible horizon boundary. Include required transitions, green holds, forced behavior, and schedule delays. Let `T` be elapsed seconds and `d = 0.95 ** (1/5)`:

```text
R = sum(d ** (i - 1) * r_i, i = 1..T)
target = R + d ** T * max_allowed Q(next_state, action)
new_Q = old_Q + 0.1 * (target - old_Q)
```

Read all target values before writing the original entry. Initialize missing bootstrap entries to zero only in training. Increase coverage/update counts only after a valid update completes. Preserve full precision internally and round only display text. Modules 5 and 6 define horizon handling, discarded incomplete feedback, and checkpoint persistence. No updates occur during yellow or evaluation.

### Decision evidence and acceptance

Return a decision ID, observation reference, target direction, selection source, raw scores/bins, decision-time Q-values and counts, epsilon/branch in training, tie details where relevant, and constraint/fallback reason. Record learning evidence later under the same originating decision reference. Do not infer confidence or causality from a Q-value gap.

Acceptance: verify bin boundaries, tie handling, exploration branches, accumulated-duration arithmetic, update-before-next-selection ordering, shared timing gates, coverage-aware fallback, evaluation immutability, and preservation of decision-time evidence after learning. Use small deterministic cases with known expected outcomes; do not claim that reproducing an update formula proves traffic performance.

## Module 3: Traffic-light actuator

### Responsibility

Create `TrafficActuator` in `src/traffic_cps/actuator.py` to validate and submit logical direction requests to the adapter and report submission status.

The controller determines the requested direction. The actuator validates, translates, and submits it through the simulation adapter. The adapter reports actual signal behavior as simulation time advances. The actuator does not independently select a traffic policy, advance simulation time, or declare a transition complete merely because submission succeeded.

### Input and output

Input: an explicit request for N/S or W/E green, linked to its originating decision record and target signal. Resolve the direction through the validated direction-to-signal-mask mapping built at startup from `getControlledLinks` (build contract, Section B); do not assume a particular mask string or link order names a direction.

Output: a submission record containing the decision reference, requested direction, validated target direction/mask mapping when available, submission time, success/failure status, and an error reason when applicable. Successful submission means the request reached the adapter's command interface, not that the requested green is already active.

### Request handling

1. Receive the controller request and retain its decision reference.
2. Validate the request and its configured signal/phase mapping.
3. For a valid request, translate the direction to a simulator signal-mask mapping and submit it through the adapter.
4. Record whether submission succeeded or failed.
5. Let the adapter's observed signal states establish when yellow and the requested green actually occur. Link those events to the original decision.

Reject an invalid or missing request with an explicit error. Notify the runtime so it can stop the run cleanly, preserve records, and mark the result incomplete. Do not silently default to phase zero or remap an invalid number using modulo arithmetic. Retrying an invalid command is not the sensor's transient-retrieval retry policy.

The adapter enforces the shared timing and transition rules. Module 4 defines the agreed handling of timing-blocked requests, command failures, and uncertain delivery. A submitted command is not proof of execution.

### Explainability contribution

Keep these events distinct: the controller requested a direction, the actuator submitted the command, and the simulator later showed the requested green. A truthful explanation can distinguish a controller decision, a submission problem, and a transition still in progress.

### Acceptance checks for implementation

- Valid N/S and W/E requests map to the correct configured signal masks and preserve the decision reference.
- Invalid, missing, or unmapped requests produce an explicit error without submitting a substituted command.
- Submission success is never reported as confirmation that the requested green is active.
- Submission errors retain their cause and are visible to the runtime.
- A request that starts a yellow transition remains traceable through the adapter's subsequent observations.

## Module 4: Simulation adapter

### Responsibility

Create `SumoAdapter` in `src/traffic_cps/adapter.py`. Connect directly to SUMO through one owned TraCI connection.

- Start, reset, and close the simulator.
- Receive actuator submissions and enforce the shared signal timing and transition rules.
- Advance simulation time when requested by the runtime.
- Expose traffic readings, actual signal colors, and timestamped transition events for the sensor, learning, and explanations.

The adapter is the authoritative owner of timing and transition state. Controllers use its information rather than duplicating transition timers. It does not select queue-based or learned preferences. Enforce timing in one consistent integration path rather than layering conflicting independent schedules over the simulator.

### Execution and result reporting

For an accepted switch, execute the configured sequence: existing green, yellow with the conflicting direction still red, and the requested green. Observe and record actual changes, including changes between controller decision points, and link events to the originating decision. A request to retain current green must not reset its elapsed-green clock.

Distinguish these outcomes explicitly:

| Outcome | Adapter/runtime response |
|---|---|
| Valid request submitted successfully | Report submission; confirm actual execution through subsequent observations. |
| Valid request blocked by timing | Preserve required signal behavior, report the reason, and discard the request. Continue the run. |
| Conflicting request during a transition | Complete the committed transition without retargeting or queuing the new request. Report the block. |
| Invalid or missing request | Report an error and stop the run cleanly. Do not substitute another phase. |
| Simulator command failure or uncertain delivery | Stop issuing commands and end the run cleanly. Do not blindly retry. Preserve the distinction between last verified state and unconfirmed execution. |

A discarded request never executes merely because its timing restriction later expires. At a subsequent eligible control point, the runtime obtains fresh observations and the controller makes a new decision.

### Clean shutdown and learning integrity

The adapter reports failures; the runtime coordinates shutdown and asks the persistence layer to preserve results. Required sensor retrieval follows the separate two-retry policy before stopping. Report the last completed step separately from an attempted or failed step, and mark the run incomplete.

Keep Q-table updates from valid, completed action outcomes. Discard unfinished feedback intervals from learning while retaining their diagnostic traces. Do not turn an error into a synthetic terminal reward. On clean shutdown, save the last valid learned table, completed-update counts, experience metadata, and exploration progress. A new training episode resets traffic and clears pending actions and feedback, while retaining saved learning. Evaluation never changes the training table or progress.

Partial traffic metrics remain labeled as partial and are not presented as completed-episode benchmarks. Saving on clean shutdown depends on successful persistence; abrupt crashes and checkpoint-write failures require separate handling in that module.

### Explainability contribution and acceptance checks

- Trace a controller request through submission, observed yellow, and observed green without equating submission with execution.
- Verify that a minimum-green block does not schedule a delayed command.
- Verify that a conflicting request cannot retarget a transition.
- Verify that retaining green does not reset its timer.
- Inject a command error and verify that no automatic resubmission occurs and the runtime marks the run incomplete.
- Verify that completed training updates survive clean shutdown, while an unfinished action contributes no partial update and is not carried across reset.
- Keep evaluation checkpoints unchanged on both normal and error exits.

Module 2A defines signal timing. Module 5 defines sampling, control scheduling, and duration-aware reward accounting.

## Module 4A: Safety monitor

### Responsibility

Independently check the signal-safety invariants on every command and every observed second, separate from the adapter code that schedules phases. A separately implemented checker can veto low-level signal commands. This is a runtime assurance guard, not a claim of a complete or formally verified Simplex architecture with a backup controller. The adapter remains the owner of timing; the monitor checks proposed commands and observed states for violations of the defined invariants.

### Invariants

| ID | Invariant | Checked |
|---|---|---|
| S1 | At most one direction group is non-red (green or yellow) at any time. | Every observed second, from raw simulator signal-state strings, not from the adapter's internal phase variables. |
| S2 | Except for the explicitly validated time-zero N/S initialization, a direction becomes green only after the opposing direction has shown yellow for at least two seconds and becomes red in the same transition. | Every observed signal change. |
| S3 | No optional switch leaves a green before minimum green has elapsed. | Every submitted command and observed yellow onset. |
| S4 | Before the horizon, when the green-service limit is reached with measured competing demand, the switch begins within one observed second. Suppress new commands at the horizon. | Every observed second. |

### Behavior

- **Pre-command check:** Before any low-level mask is written (including automatic yellow completion or a forced switch), the monitor evaluates the proposed mask against its own raw signal history for S1 to S3. If the proposed command would violate an invariant, it blocks the command and records a `safety_override` event linked to the decision. A block here indicates a controller or adapter bug and is logged as an integration diagnostic, like the unexpected timing block in Module 5.
- **Post-observation check:** If an observed state violates any invariant, record a `safety_violation` event with the raw signal states, stop issuing commands, end the run cleanly, and mark it incomplete. Never continue a run after an observed violation.
- The monitor does not select actions, modify learning, or retry commands.

### Explainability contribution

Overrides and violations are first-class explanation sources: "The safety monitor blocked a switch to W/E at 37s because N/S had been green for only 3 seconds (minimum 5)." Explanations must distinguish a controller decision, an operating-rule action, and a safety-monitor intervention.

### Acceptance checks for implementation

- Fault injection: a deliberately faulty adapter path that switches directly from green to the opposing green without yellow is caught by S1/S2, the run stops, and the violation is logged with raw states.
- A faulty controller that requests switches every second is blocked by S3 pre-command, with linked override events and no executed violation.
- A delayed forced switch beyond one second is flagged by S4.
- Normal runs of all three policies produce zero overrides and zero violations; report these counts in Module 8.
- The monitor's checks read simulator state independently of the adapter's timers.

## Module 5: Runtime orchestration

### Responsibility and fixed intervals

Create `Runtime` in `src/traffic_cps/runtime.py` to coordinate sensing, controller selection, actuation, simulation advancement, learning, and recording. Use the ordered runtime algorithm in the standalone build contract.

| Event | Agreed schedule |
|---|---|
| Simulation advancement and observation | One simulated second at a time, with a valid initial observation at time zero. |
| Scheduled control point | At fixed multiples of five simulated seconds, starting at zero and strictly before the episode horizon. |
| Free controller choice | Only at a scheduled control point when the timing state allows a choice. |
| Q-table update | In training, complete the previous accepted action's normal feedback interval at the next scheduled control point where a fresh policy choice is allowed, and update before selecting that next action. The approved duration-aware calculation, cutoff rules, and blocked-request handling are specified below. |

The schedule is anchored to episode time, not to the last decision. A skipped control point does not shift subsequent points. For a 3,600-second episode, control points are 0, 5, 10, ..., 3,595: 720 scheduled checks alongside 3,600 one-second advances. The final observation at 3,600 completes the episode without issuing another action. Not every scheduled check is a free choice or a learning update.

### Timing gates and requests

Continue collecting observations and feedback between control points, including during yellow. At a control point, consult the adapter's authoritative timing state. Skip policy selection when minimum green is incomplete or a transition is underway, and record that timing reason without incrementing the eligible-choice counter or decaying epsilon.

When a choice is allowed, obtain a fresh decision from current observations. A valid request unexpectedly blocked by the adapter is logged and discarded, not queued for later. Recheck at the next scheduled control point. Preserve an accepted transition's committed target through completion; that target is distinct from a future queued decision.

Close and update any valid previous interval before selecting a new request. If that new request is defensively blocked, do not undo the completed previous update, create a new learning interval, increment accepted-choice progress, or decay epsilon. Continue recording observations until another request is accepted; observations with no pending accepted action are not assigned a fictitious learning action. The random draw that produced a rejected request is not rolled back. Log such unexpected blocks as integration diagnostics rather than treating them as routine timing holds.

Operating constraints remain enforced independently of optional policy checks. The five-second cadence does not authorize extending a mandatory timing limit merely to wait for the next policy opportunity. Forced-switch feedback follows the agreed handling below.

### Normal feedback interval

Remember the observed starting state, accepted action, decision reference, and acceptance time. Collect subsequent one-second queue observations through execution, including yellow and the new green's required minimum duration. End the normal interval at the next fixed five-second control point where the adapter permits a fresh policy choice. A green signal alone is insufficient: minimum green and other operating constraints must allow that choice.

In training, use the completed interval's feedback and current decision state to update the original state-action entry before selecting the next action. Evaluation records the same outcome evidence without changing Q-values. Retaining the existing green uses the same boundary and normally permits completion five seconds later. Do not create an interval for a blocked request as though it executed.

Illustrative sequence using the finalized two-second yellow and five-second minimum green: an accepted switch at 10s produces the requested green at 12s. The 15s control point is skipped because only three seconds of green have elapsed. Minimum green finishes at 17s, but the fixed schedule waits until 20s. At 20s, update the state-action entry from 10s, then choose again. Continue collecting observations during the three-second scheduling delay. This is a design example, not a measured run.

An intervening forced switch extends the pending interval as specified below. Planned experiment cutoffs follow the episode-ending rule below. This version supports fixed-horizon runs only, not separate natural terminal episodes. On measurement or execution failure, retain completed valid updates and discard the unfinished learning interval under the existing clean-shutdown policy.

### Forced switches and learning feedback

If the green-service limit is reached while the competing direction has queued vehicles, the adapter initiates the required switch without waiting for the next five-second policy check. Existing transition and minimum-green priority rules still apply. Record the forced-switch reason, timestamp, observed demand, timing limit, and actual transition events separately from controller choices.

If an accepted controller action has a pending feedback interval, keep its original state/action reference and continue collecting one-second rewards through the forced transition, required green, and any remaining wait until a scheduled check allows a fresh policy choice. At that check, update the original entry in training before choosing again. Include the full elapsed interval in duration-aware reward accounting; do not restart the interval or discard its earlier rewards when the forced switch begins.

The forced switch creates no new policy decision or independent state-action learning interval, does not increment the eligible-choice counter, and does not advance exploration decay. No Q update occurs merely because the forced switch begins or while yellow is active. If there is no pending controller action, record the forced behavior without inventing a previous action to update. Evaluation records the same evidence without learning.

The learned value reflects the original choice's outcomes within a system that enforces operating rules. Explanations must distinguish the controller's request from the forced switch and must not claim that the controller selected the forced direction or alone caused all observed congestion. Existing failure-stop behavior still discards an unfinished learning interval; planned episode cutoffs follow the rule below.

### Planned episode cutoff

Treat the configured 3,600-second horizon as an experiment cutoff, not evidence that future traffic rewards are zero. Collect the final observation, issue no new signal request, do not extend simulation time, and save the learned table, progress metadata, observations, and run summary.

- If a controller action is pending and the final scheduled-grid boundary would allow a fresh policy choice, complete its feedback interval and perform the training update before shutdown. Use the final state as the next state and retain its best allowed Q-value in the target, even though no subsequent action is executed in this run.
- If yellow, minimum green, or another operating constraint prevents a fresh choice, preserve the interval's diagnostic observations but discard that unfinished interval from learning. Keep all earlier valid updates. A cutoff away from the fixed scheduling grid does not create a new learning boundary.
- If no action is pending, there is no final update. Evaluation never updates the table.

Reaching the planned horizon successfully is a completed simulation run even if its final learning interval is unfinished. A measurement or execution failure remains an incomplete run. Save failures remain explicit persistence failures, not successful saves. Clear pending actions and feedback before resetting traffic for another episode; retain the Q-table, training counts, epsilon, and eligible-choice counter. Never connect an old episode's pending action to a new episode's initial traffic.

True terminal scenarios, such as ending an episode early when all vehicles depart, are outside this version. Continue observing an empty network to the configured horizon rather than treating emptiness as a terminal reward. If the underlying simulator unexpectedly cannot advance before the horizon, report an incomplete run; do not invent a terminal update.

### Accumulated congestion reward

Calculate each one-second reward as `r_i = -(ns_queue_score_i + we_queue_score_i)` using unrounded scores. Accumulate feedback across the entire accepted action interval, rather than using only the final sample or averaging the samples. Include yellow, required green, and the wait until the next eligible scheduled control point. Longer congestion therefore contributes additional penalties; there is no separate explicit switching penalty. Raw queued vehicle counts remain visible alongside the scores.

For an interval starting at time `t` and ending at `t + T`, use subsequent observations at `t + 1, ..., t + T`. The observation at `t` defines the starting state; the closing observation contributes the last reward and the next state. Do not count that same reward again in the next interval.

**Discount time unit:** Use `gamma_5 = 0.95` per five simulated seconds, with one-second multiplier `d = gamma_5 ** (1/5)`. Then `R = sum(d ** (i-1) * r_i for i in 1..T)` and `target = R + d ** T * max_allowed Q(next_state, action)`. Update the original entry using `new_Q = old_Q + 0.1 * (target - old_Q)`. Use next-state values as they stand before this update. The accumulated reward measures the observed interval; the next-state Q-value estimates the discounted return beyond it. The two terms cover successive periods, not duplicate observations. Checkpoint metadata must use these elapsed-time discount semantics.

Record the sample references, interval duration, raw reward sum, discounted accumulated reward, discount time unit, bootstrap multiplier, next-state values, target, and old/new Q-values. The numerical worked example in the interview Q&A illustrates the approved time unit, not measured performance.

### Explainability and progress counters

Keep simulation ticks, scheduled control points, eligible choices, accepted requests, and completed learning updates distinct in records and summaries. A repeated state can receive many updates; a control point does not imply a newly populated table entry.

Log actual signal changes at the observation cadence so a yellow interval is not lost between five-second control points. This cadence supports the current two-second yellow and the discussed three-second example; do not claim it captures arbitrary sub-second changes without additional event instrumentation.

### Acceptance checks for implementation

- Advance and observe at one-second increments while keeping control points anchored to multiples of five.
- A skipped or blocked request does not shift the schedule or create an automatically executable deferred request.
- No policy selection or exploration-counter advancement occurs during a timing-forced hold or ongoing transition.
- Preserve observation and transition records between control points.
- Do not update a blocked request's Q-value as if it executed, and do not perform Q updates during yellow under this design.
- Do not issue an action at the episode horizon.
- For the illustrative switch at 10s, do not update at 12s, 15s, or 17s; complete the normal interval at 20s and update the original state-action entry before the next choice.
- Include observations during the wait between minimum-green expiry and the next eligible scheduled check in feedback evidence.
- A service-limit switch between policy checks executes under the adapter's timing rules without waiting for the next policy check; its recorded source is the operating constraint.
- A forced switch preserves the pending original state/action and accumulated rewards until the next eligible choice, without creating a new policy decision or decaying epsilon.
- A forced switch with no pending controller action produces diagnostic records but no fabricated Q update.
- At the 3,600-second horizon, a pending interval completes only if timing permits a fresh choice; training retains the next-state estimate in its target but submits no further action.
- A successful horizon cutoff during yellow or minimum green preserves traces and completed updates, drops unfinished feedback, and remains a completed simulation run.
- Episode reset clears pending feedback while retaining saved learning and exploration progress.

**Runtime design status:** Complete for fixed-horizon experiments. Blocked-request attribution is specified above; natural early-terminal episodes are outside scope. The five-second minimum-green parameter remains independent of the five-second policy schedule.

## Module 6: Event logging and training checkpoints

### Separate history from saved learning

Use a separate JSONL event log for each simulation run and a JSON checkpoint for saved training progress. The event log supports reconstruction and explanations; the checkpoint restores the controller's learned data into memory. The running controller consults its in-memory Q-table, not the checkpoint file on every decision. Episodes within the same running application can retain that table in memory without reloading it.

Illustrative output layout, scoped within a training experiment so independent experiments do not overwrite one another:

```text
outputs/<training_id>/
  runs/
    run_001/events.jsonl
    run_002/events.jsonl
  checkpoints/
    training_latest.json
    training_previous.json
```

### Event-log structure

Each line is one JSON event. Record one-second observations and events when they occur: controller decisions, submission results, actual signal changes, forced actions, learning updates, and failures. Include event type, simulation time, and references linking decisions to their observations and outcomes. Identify training/run context through the containing run and record metadata. Evidence remains distinct from later formatted explanations.

Simplified excerpt, not a complete trace or final exhaustive schema:

```jsonl
{"type":"observation","id":"obs_10","time":10,"waiting_vehicles":{"NS":8,"WE":6},"queue_scores":{"NS":0.8,"WE":0.6},"current_green":"WE","green_elapsed":10}
{"type":"decision","id":"decision_2","time":10,"observation_id":"obs_10","requested_green":"NS","source":"exploitation","q_values":{"NS":-15,"WE":-18},"reason":"Highest Q-value"}
{"type":"signal_change","time":10,"decision_id":"decision_2","lights":{"NS":"red","WE":"yellow"}}
{"type":"signal_change","time":12,"decision_id":"decision_2","lights":{"NS":"green","WE":"red"}}
```

The full observation includes the sensor module's per-lane counts, actual colors, and durations. Full learning records contain the agreed interval rewards, discount settings, target, and old/new Q-values. The excerpt omits intervening observations and submission records for readability. Log observed signal changes as execution evidence, not merely a prediction from a request.

### Checkpoint contents

Save one consistent snapshot of the Q-table and supporting training progress:

- Checkpoint format version and training identity.
- State-action Q-values, training visit counts, and completed-update counts.
- Exploration probability, its decay/floor settings, and eligible-decision counter.
- Episode progress, distinguishing completed episodes from interrupted runs, and the persistent episode-attempt index used to select seeds and assign new run identities.
- Controller random-generator state and its compatible generator/version metadata; record each episode's traffic seed and configuration in run metadata.
- Learning rate, reward definition, discount factor and time unit.
- State encoding, queue and elapsed-time bin thresholds, action mapping, lane mapping, signal durations, and relevant configuration/version metadata for compatibility checking.

Illustrative excerpt; omitted fields above are required in the complete checkpoint:

```json
{
  "format_version": 1,
  "training_id": "training_001",
  "completed_episodes": 3,
  "learning_rate": 0.1,
  "discount": {"factor": 0.95, "time_unit_seconds": 5},
  "q_table": [
    {
      "state": {"ns_queue_bin": 2, "we_queue_bin": 1, "current_green": "WE", "elapsed_green_bin": 1},
      "q_values": {"NS": -14.7573, "WE": -18.0},
      "action_visit_counts": {"NS": 12, "WE": 7}
    }
  ]
}
```

### Checkpoint schedule and replacement

**Approved:** September 30, 2026. Save a complete training checkpoint after every 100 completed Q-table updates, at each episode end, and during controlled shutdown. Count actual completed training updates, not simulation ticks, policy checks, or unique table entries. Count progress since the last successful checkpoint; an episode-end or shutdown save includes that progress. Evaluation does not save changes to the training checkpoint. Save all existing entries, including zero-valued entries and their visit counts, rather than filtering by nonzero Q-values.

Take a consistent snapshot after a completed update. Write the new checkpoint to a temporary file in the same directory, finish and close the write successfully, then atomically replace the latest checkpoint. Do not overwrite the current checkpoint in place or publish a partially written temporary file as the latest checkpoint. Do not mark progress saved until replacement succeeds. Synchronize checkpoint creation with updates so Q-values, counts, and exploration progress describe the same snapshot. Detailed disk-durability handling remains part of persistence implementation; atomic replacement alone is not a guarantee against every power-loss scenario.

On controlled shutdown, include valid completed learning and exclude unfinished feedback from learned updates. Unexpected termination restores only the last successful checkpoint. Under normal operation with successful scheduled saves, fewer than 100 additional updates accumulate between saves; a crash during a due save or a write failure can lose 100 or more updates. Do not present the earlier approximate 99-update loss estimate as an unconditional guarantee.

### Write-failure policy

**Approved:** September 30, 2026. If an event-log or checkpoint write fails, stop advancing the simulation and issuing signal requests, notify the user directly through application output, and perform a controlled shutdown. Do not silently continue a run with unavailable explanation records or failed training persistence. This policy is separate from the sensor's two retrieval retries.

| Failure | Response |
|---|---|
| Event-log write fails | Preserve existing records without assuming the failed record was fully written. Notify through application output rather than relying on the failed log. Attempt one shutdown checkpoint of valid completed learning if checkpoint storage remains available; report its actual outcome. |
| Checkpoint write fails | Preserve the previous valid checkpoint under the temporary-file replacement policy. Record the error in the event log if it remains writable, notify through application output, and stop. Do not claim the unsaved newer snapshot is recoverable. |

Do not automatically repeat a failed log write: it may have partially succeeded. Do not turn shutdown into a retry loop on the failed checkpoint destination. If the log-failure shutdown checkpoint also fails, report that failure and identify the last successfully saved checkpoint, if any. Loading an older checkpoint requires explicit selection under the recovery rule below. Error reporting must not recursively attempt writes to a known-failed log.

Earlier complete observations and valid Q-table updates remain valid; distinguish that validity from whether they were successfully saved. Preserve diagnostic evidence where storage still works, but exclude unfinished feedback from learning. If no prior successful checkpoint exists, report that there is no saved training state to restore rather than claiming one is intact. If checkpoint publication itself has an uncertain outcome, report it as unconfirmed rather than asserting that a particular snapshot was saved.

Report execution status and persistence status separately. A write failure before the horizon interrupts the simulation. If the final save fails after the horizon was reached successfully, report “simulation completed, saving failed.” An event-log failure also means the trace is incomplete, even if a checkpoint of completed learning is successfully saved. Never report a successful save without confirmation.

### Flushing, recovery, and randomness

**Approved:** September 30, 2026.

- **Log flushing:** Write the observations and events for each simulated second, then flush before the next simulation advance. Flush remaining records during normal shutdown. Flush failures follow the write-failure stop policy. Flushing moves application buffers to the operating system; it does not promise power-loss durability.
- **Checkpoint load failures:** If a requested resume checkpoint is missing, corrupt, or incompatible, stop before starting traffic and report the specific problem. Never silently initialize an empty table. Explicit fresh training creates a separate training folder.
- **Recovery retention:** Keep the latest and one previous valid checkpoint when available. Fully write and validate the new snapshot before rotating saved files. Preserve a valid recovery copy throughout replacement; do not overwrite the backup with a corrupt latest file. A failed latest load may identify a validated, compatible previous checkpoint as an available option, but must not load it automatically. Record the explicitly selected checkpoint's identity when resuming.
- **Randomness:** Save and restore the controller's random-generator state consistently with the table and exploration progress. Record each episode's traffic seed and configuration in run metadata. Start fresh traffic on resume; restoring controller randomness does not restore vehicles, replay the interrupted episode, or guarantee an identical full trajectory across changed execution environments.

### Restoring training

1. Read the selected checkpoint and validate its format and compatibility with the intended experiment. Missing, unreadable, or incompatible data stops resume before traffic starts; any older recovery checkpoint must be explicitly selected.
2. Reconstruct the in-memory Q-table and visit/update counts. Restore exploration probability, its decision counter, and the controller's random-generator state together with the agreed learning configuration; do not silently reset exploration to 100%.
3. Initialize a fresh traffic episode and clear old pending requests, previous-action feedback, and transient signal state. Start a new run log and record which checkpoint supplied the learning state.
4. At each eligible control point, use the restored table for action selection. Subsequent valid outcomes continue updating those entries in training. Evaluation loads the saved values but leaves training state unchanged.

This resumes controller learning from the explicitly selected valid snapshot, normally the latest successful save. It does not replay the event log, reconstruct the old traffic scene, or resume an interrupted episode at its old simulation second. Updates made after that save are not restored. Restored controller randomness preserves its generator sequence under matching conditions, not an identical future traffic trajectory.

### Acceptance checks for implementation

- Parse each event line independently and follow observation → decision → submission → actual signal change references.
- Saving and loading a checkpoint preserves Q-values, visit/update counts, epsilon, and eligible-decision progress together.
- An episode reset preserves learning but clears unfinished feedback and starts a new run log.
- A resumed run records checkpoint provenance; incompatible configuration is not silently treated as compatible training.
- Evaluation cannot overwrite training progress or introduce learned entries into the saved table.
- The 100th completed update since the last successful save triggers a checkpoint; episode-end and controlled-shutdown saves preserve progress even below that threshold.
- A failed temporary write leaves the previous latest checkpoint intact; only a fully written snapshot is published by replacement.
- A checkpoint preserves zero-valued entries and visit counts, and loading reproduces the saved table without filtering by value.
- A log-write failure stops further simulation work, reports outside the failed log, and attempts to preserve completed learning without retrying the failed event write.
- A checkpoint-write failure preserves the prior valid file where replacement has not occurred, stops the run, and does not recursively retry during shutdown.
- A final-save failure after the horizon reports completed simulation execution separately from failed persistence.
- If both output paths fail, shutdown terminates with direct error reporting rather than looping, fabricating a saved checkpoint, or learning from unfinished feedback.
- Missing, corrupt, or incompatible resume files prevent traffic startup and never silently reset learning or load an older checkpoint.
- Checkpoint rotation preserves a valid recovery copy; explicitly selecting an older checkpoint records its provenance.
- Each simulated second's events are flushed before further advancement; a flush failure triggers controlled shutdown.
- Save/load preserves controller random-generator state under a matching generator implementation, and run metadata records the traffic seed and configuration.

Implement and verify the event schema, snapshot validation, safe file rotation, and acceptance checks.

## Module 7: Explanation layer

### Responsibility and principles

Convert linked event evidence into readable, template-based explanations. This layer does not select actions, modify the Q-table, or use an LLM to invent reasons. It describes the recorded decision process and observed execution.

The design is mapped to NIST's [Four Principles of Explainable Artificial Intelligence](https://nvlpubs.nist.gov/nistpubs/ir/2021/NIST.IR.8312.pdf): provide evidence or reasons (explanation), make explanations understandable to intended users (meaningful), reflect the actual output-producing process (explanation accuracy), and recognize limits of applicability or knowledge (knowledge limits). The mapping was developed after proposing the components; NIST does not prescribe this exact component list. It is a design rationale, not a claim of certification or demonstrated compliance.

### Intended reader and design approach

NIST's meaningfulness principle depends on the audience, so name one. **Primary reader:** a traffic or controls engineer inspecting and debugging the controller's behavior. Templates, units, and the run report are written for that reader. A separate lay-audience interface is outside this implementation.

**Interpretable by design, not post hoc:** Both controllers are directly inspectable. The queue rule is a comparison, and the Q-learning policy is a 200-state lookup table with an argmax. Explanations therefore read the actual decision rule and values; no surrogate model or post-hoc attribution method approximates the policy. This is the project's central explainability claim (compare Rudin, 2019, "Stop explaining black box machine learning models for high stakes decisions and use interpretable models instead"). It holds only for this tabular design; replacing the table with a neural network would remove it.

### Terminology for readers

- Present Q-values as **expected future congestion cost** (the negated Q-value, lower is better), in units of discounted queue-score-seconds at the fixed one-second sampling resolution. Raw negative Q-values remain in the supporting evidence.
- Use **queue score** only for the sensor's normalized measurement. Do not call Q-values "scores" or "learned scores".
- Retain decisions are explained as decisions ("Kept N/S green because..."), not omitted. They will be the majority of decisions.

### Explanation components

| Component | Contract |
|---|---|
| Input evidence | Read linked observations, decisions, actuator submissions, actual signal changes, operating-rule events, and learning updates. Preserve source event references and simulation times so claims can be checked. |
| Readable output | Produce a plain-language account of each decision or operating-rule event using deterministic templates. Keep the selection explanation distinct from subsequent execution and learning. |
| Vehicle counts | Show waiting vehicles by N/S and W/E, with the sensor's per-lane counts available in supporting evidence. Show the technical scores actually used alongside readable counts. Counts provide context; do not substitute them for the selection inputs. |
| Decision reason | Use the recorded branch: queue comparison, Q-value exploitation, random exploration, unseen-state fallback, tie-breaking, or operating constraint. State the determining reason rather than choosing a plausible explanation after the fact. |
| Execution status | Distinguish requested, submitted, timing-blocked, transitioning, observed green, failed, and unconfirmed states as supported by records. Submission is not proof of execution. |
| Learning details | After a valid feedback interval completes in training, separately show the originating state/action, interval, accumulated reward, old Q-value, next-state estimate, target, discount settings, and updated value. Preserve the original decision-time Q-values. Evaluation does not invent learning updates. |
| Limitation indicator | Explicitly identify unseen-state fallback, missing evidence, incomplete outcomes, or unconfirmed execution. Say which portion cannot be explained rather than guessing. Do not treat visit counts or Q-value differences as calibrated confidence. |

Vehicle counts and readable wording aim at meaningfulness; branch-specific reasons and time-linked execution evidence aim at explanation accuracy. Input references provide evidence and traceability. Limitation indicators address part of knowledge limits; they do not establish that the learned policy is reliable in every observed state.

### Templates and evidence boundaries

| Recorded source | Illustrative wording |
|---|---|
| Queue-based selection | “Requested N/S because its queue score was higher: 0.8 versus 0.6.” |
| Q-learning exploitation | “Requested N/S because its expected future congestion cost was lower: 15 versus 18.” Raw Q-values (-15, -18) in evidence. |
| Queue-based retain | “Kept N/S green because its queue score was higher: 1.1 versus 0.4.” |
| Q-learning retain | “Kept N/S green because keeping it had a lower expected future congestion cost: 12 versus 16.” |
| Exploration | “Randomly selected N/S to gather training experience.” |
| Unseen-state fallback | “No training experience was recorded for this state, so the queue-based rule selected N/S.” Include the recorded queue comparison or tie reason. |
| Incomplete-action-coverage fallback | “Not all available actions have completed training experience in this state, so the queue-based rule selected N/S.” Include the recorded queue comparison or tie reason. |
| Tie | “The highest Q-values were tied, so the controller retained the current green.” Use the queue-tie template for a queue-based decision instead. |
| Minimum-green hold | “Kept W/E green because its minimum green duration had not elapsed.” This describes a constraint, not a new policy choice. |
| Forced switch | “Switched to N/S because W/E reached its service limit and vehicles were waiting on N/S.” Use requested/transitioning wording until execution is observed. |

Illustrative linked account: “At 10s, N/S had 8 waiting vehicles and W/E had 6. The controller requested N/S green because its expected future congestion cost was lower: 15 versus 18. W/E entered yellow at 10s, and N/S became green at 12s.” Only include the final execution statement once its confirming event exists; do not claim to know it at 10s. These numbers are examples, not experiment results.

If exploration selected N/S, never explain it as a queue-driven or greedy choice just because N/S also had more vehicles or a higher Q-value. If an operating rule forced the outcome, do not describe it as a learned preference. If evidence is missing, show “explanation unavailable” for the affected part and identify the missing record or field; preserve supported parts of the account. Missing log evidence does not by itself prove a sensor reading or physical execution failed.

“Higher Q-value” explains selection, not the complete history of how that value was learned. Learning records expose individual updates over time without proving that one choice alone caused the observed traffic or that the chosen action was globally optimal. Explanations must not use post-update values to justify a prior choice.

### Global policy view

Per-decision explanations are local. Add a global view of the tabular action preferences and coverage, while explicitly marking constraints that bins alone cannot resolve:

- For each combination of current green and reachable elapsed bin, render a 5 × 5 grid (N/S queue bin × W/E queue bin) showing the greedy action, the cost gap between actions, and per-action completed-update counts. Mark cells that would use evaluation fallback (unseen or incomplete coverage) distinctly, and grey out unreachable cells.
- Generate the view from a specific checkpoint and record its identity. Produce it for the episode 5, 10, and 15 checkpoints so readers can see the policy change over training.
- Compare against the queue rule only where unequal queue bins establish its ordering. Equal-bin comparisons are ambiguous because raw scores can differ; do not label those cells as exact disagreements. Mark elapsed-bin-3/competing-bin-0 eligibility as conditional on an exactly empty competing queue. The build contract defines these labels.

### Contrastive explanations

People typically ask "why this rather than that" (Miller, 2019, "Explanation in artificial intelligence: Insights from the social sciences"). Every policy explanation states the alternative and its value when supported. For covered exploitation decisions, compute a minimal greedy-policy counterfactual from the decision-time table. For exploration, fallback, or constraints, explain the actual branch and do not pretend a greedy counterfactual determined the choice:

- Hold actual current green and exact elapsed time fixed. Search permitted queue-bin combinations with complete coverage by increasing Manhattan bin distance, breaking equal distances by NS bin then WE bin. Apply the conditional eligibility rules in the build contract.
- Template: “Under the greedy table, W/E would be preferred if its queue category were High (bin 3) instead of Medium.” List every changed category and any zero-demand assumption. If no supported candidate exists, say so.
- For the queue rule, state the raw threshold including tie tolerance and current-green behavior; see the exact comparison in the build contract.
- A counterfactual describes the decision rule, not a causal claim about traffic. It must use decision-time table values, never post-update values.

### Viewing surface and summarization

Explanations must be visible without reading JSONL. Provide, generated offline from the event log and checkpoint only:

- **CLI:** `explain --run <run_id> --decision <decision_id>` prints the linked account (observation, reason, alternative, counterfactual, execution status, later learning update); `summarize --run <run_id>` prints the episode summary.
- **Static HTML run report:** a timeline of per-direction waiting vehicles, signal-color bands per direction, and decision markers colored by source (queue rule, exploitation, exploration, fallback, operating rule, safety monitor). Each marker links to its explanation. Include the global policy view for the run's checkpoint.
- **Episode summary:** counts by decision source with denominators, switches, forced actions, fallbacks, exploration, safety overrides and violations, plus a short list of notable decisions (switches, forced actions, fallbacks, exploration, disagreements with the queue rule). Up to 720 scheduled checks per hour, with fewer actual choices, can produce too many events to read individually; the summary directs attention.

This static report does not change the out-of-scope status of a live dashboard.

### Acceptance checks for implementation

- Each explanation's reason matches the recorded selection or constraint branch, including ties, exploration, and fallback.
- Numbers and timestamps match linked records; no generated claim lacks supporting evidence.
- Requested/submitted actions are not described as observed green before confirmation.
- An exploration event is still explained as exploration when it happens to choose the highest-valued action.
- Incomplete evidence produces an explicit limitation rather than invented values or a guessed reason.
- Decision-time evidence remains unchanged when later learning updates arrive, and evaluation produces no fictitious updates.
- Q-values are presented to readers as expected future congestion cost with raw values in evidence; no template calls them scores.
- Retain decisions produce explanations with the same fidelity checks as switches.
- The global policy view matches the checkpoint's greedy actions, coverage, and fallback markers cell by cell.
- Each counterfactual, when recomputed from the decision-time table, actually yields the stated alternative action, and no closer differing state exists.
- The run report's markers, colors, and links match the event log; every marker resolves to an explanation or an explicit "explanation unavailable".

Readable templates expose decision evidence; they do not by themselves demonstrate improved user understanding.

## Module 8: Experiment runner and evaluation

### Run setup and training budget

Provide runner modes for fresh training, resumed training, fixed-time evaluation, queue-based evaluation, and frozen Q-learning evaluation. Use one shared component assembly path with configuration selecting the controller and mode. Validate configuration, lane/phase mapping, output location, and checkpoint compatibility before advancing traffic. Close SUMO and output handles on every exit path.

Initial training budget: 15 episodes of 3,600 simulated seconds, GUI off, one-second observations, five-second policy checks, and timing defaults 2/5/60 seconds. Use traffic seeds 0 through 14 and a separately seeded controller generator (initial seed 0), recording the generator implementation and all seeds. Resume retains the controller RNG state and selects the next episode seed using a persistent episode-attempt index; failed attempts have distinct run IDs and are not counted as completed episodes. Changing the budget or extending training is explicit and recorded, not driven by reaching the exploration floor alone.

Evaluate checkpoint copies after completed episodes 5, 10, and 15 on development traffic seeds 100–102. These evaluations do not consume training RNG state or mutate training checkpoints. Use episode 15 as the initial final model rather than silently cherry-picking the best development result. Report learning curves, coverage, epsilon, and fallback frequency, and render the global policy view for each evaluated checkpoint. Fifteen episodes is an initial engineering budget, not proof of convergence or sufficient coverage. Stop that experiment at its configured budget and report shortcomings; any subsequent tuning uses development scenarios and a new final evaluation set if an earlier test set has already informed choices.

### Standalone baselines

Implement all three controllers in this new project. Fixed-time and queue-based runs provide the baselines for frozen Q-learning. Historical experiments and legacy source files are neither available nor required.

### Traffic demand variety

Training, development, and final scenarios must not all share one demand level. Include at least: balanced moderate demand, asymmetric demand (one direction heavier), and a peak period within the hour. Record the demand profile per seed. Varied demand exercises more of the state space and produces explanations that differ meaningfully across situations.

### Controller comparison

Compare three policies on five held-out traffic scenarios with seeds 200–204: fixed-time, queue-based, and frozen Q-learning with its disclosed coverage fallback. Materialize each scenario's vehicle departure/route demand once and reuse it across policies. A shared seed alone is not sufficient evidence that realized arrivals were identical; record generated demand and actual insertion/arrival counts. Use the same network, initialization, horizon, observation cadence, and operating constraints for every policy.

The fixed-time baseline uses a green target per direction that must be strictly below the green-service limit, and is tuned before final evaluation (below). At an eligible five-second policy check, retain green until its target has elapsed, then request the other direction. Shared forced-service rules can act between checks, and yellow takes two seconds. Report this as a fixed-time policy with shared operating constraints, including actual realized durations. Its explanation source is `fixed_schedule`, distinct from a forced switch or queue preference.

**Why the target must be below the service limit:** the runtime enforces the service limit (step 5) before the policy check (step 6), and after the first switch green onsets fall off the five-second grid. With a target equal to the limit, nearly every fixed-time switch would be executed and explained as a forced `operating_action`, so the report would misattribute the baseline's own schedule. With target < limit, fixed-time switches occur at the first eligible check after the target and are explained as `fixed_schedule`; a forced switch in a fixed-time run then indicates a bug or a misconfiguration.

**Tuning (fair baseline):** an untuned long cycle is a strawman that inflates any adaptive controller's advantage. Before final evaluation, run fixed-time on the development seeds for each candidate in `fixed_green_candidates_s`, pick the candidate with the lowest mean queued vehicles averaged over development seeds (ties go to the shorter target), and record all candidates' results and the choice in `fixed_tuning.json`. Final and robustness suites use only that recorded value. Never tune on final seeds. This is grid-search tuning on the project's own metric, not a Webster-formula derivation; say so when presenting it. Queue-based uses Module 2A. Q-learning uses Module 2B with learning frozen. Neither baseline writes a Q-learning checkpoint.

### Metrics and reporting

| Measure | Definition/purpose |
|---|---|
| Mean queued vehicles | Primary congestion metric: sum total incoming-lane queued counts at times 1..3,600, divided by 3,600. Include zero samples; time-zero observation is context, not an extra interval. Also report by direction. |
| Peak queue and accumulated queue exposure | Maximum sampled total queued vehicles; sum of one-second queued counts in vehicle-seconds. These are not labeled per-trip waiting time. |
| Completed vehicles and remaining demand | Report completed arrivals, vehicles still in the network, and scheduled but not inserted vehicles so a low measured queue cannot hide unmet demand. |
| Controller behavior | Eligible accepted choices, actual switches, forced actions, rejected requests, exploration/exploitation branches, and evaluation fallback frequency with denominators. |
| Learning coverage | Completed-update counts by state/action, fraction of evaluation choices requiring fallback, and unvisited permitted actions. Do not demand coverage of unreachable states. |
| Q-only versus fallback | For frozen Q-learning, report metrics alongside the share of free choices made by the table versus queue-based fallback. If fallback dominates, the comparison mostly measures the queue rule; say so. |
| Safety | Safety-monitor overrides and violations per run; expected zero for all policies. |
| Execution and persistence | Wall-clock duration, simulation completion, trace completeness, checkpoint outcome, and failure reason. |

Show each seed's results, means, sample standard deviations, and paired differences against each baseline. Calculate relative improvement only when the baseline is nonzero and label the exact metric. Exclude failed/incomplete runs from complete-run aggregate comparisons while reporting their counts, causes, and partial metrics separately. Five scenarios provide initial variability evidence, not a guarantee of statistical significance or generalization. No historical improvement percentage may be presented as a result of this application. Do not relabel SUMO's instantaneous uninterrupted waiting measure as full-trip waiting time.

### Sensing robustness experiment

Run an evaluation-only sensor fault wrapper on the final scenarios for the queue-based and frozen Q-learning policies: perturb per-lane halting counts with bounded integer noise, occasionally drop a lane's reading to its previous value (stale), and recompute scores consistently from the perturbed counts. Missing readings still follow Module 1's retry-and-stop policy; this experiment covers readings that are valid-looking but wrong. Record the true and measured values each second. Report metric degradation versus clean sensing, and include in explanations, for this experiment only, the difference between what the controller saw and the simulator's truth (“Controller saw 3 waiting on W/E; actual was 7”). Noise parameters are recorded and are illustrative, not a calibrated detector model.

Run two fault levels (build contract, Section H): **low** (±1 vehicle, 5% stale) and **high** (±3 vehicles, 20% stale). With an estimated lane capacity of roughly 30 vehicles, ±1 moves a lane score by about 0.03, so the low level is expected to show little effect; the high level exists so the experiment can show degradation if it occurs. Report both, including a null result.

Highlight two contract-relevant fault cases in metrics and reports, because the safety monitor's S4 check uses measured demand by design and cannot catch them:

- **Phantom demand:** the service limit forced a switch while the true competing count was zero but the measured count was positive.
- **Masked demand:** the true competing count was positive at or beyond the service limit, but the measured count was zero, so no switch was forced.

Explain both with seen-versus-true wording, for example “Switched to W/E because the sensor reported 1 waiting vehicle; the simulator's true count was 0.”

### Planning for an unfavorable result

Exploration reaches its floor after 4,605 accepted choices, so the fraction of the 15-episode budget spent above the exploration floor depends on accepted decisions per episode; fallback may handle many evaluation choices. Q-learning may underperform the queue-based rule. That is a reportable result: present it with coverage, fallback share, and the policy-disagreement cells from the global view. Do not tune on final scenarios to avoid it.

### Explanation verification and user understanding

Programmatically replay stored evidence through the formatter and verify every factual field and branch. Use targeted cases for queue/Q ties, exploration that happens to choose the greedy action, fallback, forced switching, timing holds, delayed execution, blocked requests, missing events, and post-decision learning. Require no unsupported claims and no branch/numeric mismatches in these controlled acceptance cases. Check all generated evaluation explanations against their source references, and report mismatches, unavailable explanations, and coverage rather than hiding missing records.

This verifies explanation fidelity, not human comprehension. A human-participant study is outside this implementation. Describe the feature as exposing decision evidence in readable explanations, not as a measured improvement in user understanding.

### Completion criteria

- Component acceptance cases pass, including storage failures and one-second SUMO integration.
- Training saves compatible checkpoints and frozen evaluation leaves their contents unchanged.
- The three controllers run against matched demand plans; complete and incomplete results are clearly separated.
- Explanation fidelity checks pass or failures are clearly reported and fixed before claiming the feature works.
- Deliver configuration, per-run logs, latest/previous training checkpoints, per-seed/aggregate metrics, and an explanation report. Record code/dependency versions and configuration/demand identities.
- A clean checkout generates its network and demand and runs all baselines without any XCPS file or historical output.
- Safety-monitor fault-injection cases pass and normal runs report zero violations.
- The CLI explain/summarize commands and static run report are generated for each final evaluation run, including the global policy view.
- The sensing robustness experiment reports degradation and seen-versus-true explanations.

The document specifies the work to build and validate. Do not describe these criteria as achieved until implementation and runs provide evidence.

## Implementation order

0. Create the standalone project, install the specified dependencies, implement scenario generation, and pass the environment/network smoke checks in the build contract, including the peak saturation check (run it once the queue controller exists in step 2, and regenerate demand if it fails).
1. Implement and verify the adapter's one-second advancement, phase mapping, initialization, timers, and sensor snapshots, plus the safety monitor and its fault-injection cases.
2. **Thin vertical slice:** queue-based policy, actuator, minimal event log, a minimal explanation formatter, and a minimal timeline report, end to end on one short run. The project's core claim is visible explanations; build that path before deepening any single module.
3. Add the fixed-time policy (target below the service limit) and full runtime enforcement; test transition and failure paths and `fixed_schedule` attribution.
4. Implement tabular training/evaluation with the consolidated state, reward, interval, exploration, fallback, and cutoff contracts.
5. Complete linked event records, checkpoints, restoration, and failure-safe shutdown; use controlled fault injection for the specified failures.
6. Complete the explanation layer: all templates, contrastive counterfactuals, global policy view, episode summary, and evidence-consistency checks.
7. Tune fixed-time on development seeds, then run the configured training and comparison workflow, the two-level sensing robustness experiment, inspect failures/coverage, and produce an honest results summary.

Use unit tests for timing boundaries, Q-update arithmetic, fallback, and storage recovery; a short actual-SUMO integration run for observed transitions and time advancement; then the specified experiment suite. These checks are future implementation work, not tests already executed. Avoid adding unused L2/L3 scaffolding merely to imply a larger CPS.

**Implementation scope:** Build and validate Modules 1–8, including Module 4A and the required explanation/report features. Natural early-terminal learning, multiple intersections, road deployment, and human-comprehension studies are outside this deliverable.

## Standalone build contract

This section is normative and resolves implementation ambiguities in the earlier explanatory examples. Create every listed source/configuration/asset file in an empty repository. This document is sufficient without the interview Q&A, old XCPS modules, old SUMO assets, or downloaded checkpoints. Commands below specify the CLI to implement; they have not been implemented or executed as part of this document revision.

### A. Environment and project layout

Reference environment: Python 3.11 on Linux x86-64; macOS is supported when the matching SUMO binaries and native dependencies are available. Pin the initial simulator/toolchain to `eclipse-sumo==1.24.0`, `traci==1.24.0`, and `sumolib==1.24.0`. This is a reproducibility baseline, not a claim that 1.24.0 is newest. The releases exist: [SUMO package](https://pypi.org/project/eclipse-sumo/1.24.0/), [TraCI package](https://pypi.org/project/traci/1.24.0/), [sumolib package](https://pypi.org/project/sumolib/1.24.0/). The SUMO wheel for this release targets macOS 15+ ARM64; other Mac environments need a matching official installation rather than silently switching versions. Follow [SUMO installation guidance](https://sumo.dlr.de/docs/Downloads.php) for native dependencies.

Runtime dependencies additionally include `PyYAML==6.0.2`; development uses `pytest==8.3.5`. Use standard-library dataclasses, enums, argparse, random, json, csv, hashlib, pathlib, and HTML escaping. Static reports use locally generated HTML/SVG and optional embedded JavaScript; no external CDN, API key, Node runtime, LLM, Gymnasium, SUMO-RL, neural-network framework, or preexisting CPS package is required. Define a setuptools `pyproject.toml`, `requires-python = ">=3.11,<3.12"`, a `dev` extra for pytest, and console entry point `traffic-cps = traffic_cps.cli:main`. Record resolved dependency versions after installation and commit the resulting lock/freeze file for the reference platform.

```text
traffic-controller/
  pyproject.toml
  requirements-lock.txt             # produced after installation/verification
  README.md                         # setup, commands, limits, example report
  configs/default.yaml
  src/traffic_cps/
    __init__.py
    __main__.py                     # delegates to cli.main
    cli.py                         # doctor/scenario/run/train/evaluate/report commands
    config.py                      # safe YAML loading, validation, compatibility digest
    contracts.py                   # typed immutable records and enums
    scenario.py                    # generate all network inputs and vehicle demand
    adapter.py                     # one owned TraCI connection, masks, timers, stepping
    sensor.py                      # raw snapshot validation, counts and normalization
    safety.py                      # independent raw-state history and command veto
    actuator.py                    # logical request validation and adapter submission
    controllers/{__init__,fixed,queue,q_learning}.py
    learning.py                    # bins, pending interval, discounted update
    runtime.py                     # the sole run coordinator
    events.py                      # JSONL records, IDs, flush, failure handling
    checkpoints.py                 # safe snapshot/restore/rotation and validation
    explain.py                     # templates, policy view, contrastive queries
    report.py                      # standalone HTML/SVG and textual summary
    experiments.py                 # run suites, seeds, metrics and paired comparisons
    faults.py                      # evaluation-only noise/staleness and test injection
  tests/{unit,integration,fixtures}/
  scenarios/                       # generated plain XML, net.xml, demand and manifest
  outputs/                         # generated experiments, excluded from source control
```

Bootstrap after creating package metadata:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
sumo --version
netconvert --version
python -m traffic_cps doctor
python -m pip freeze > requirements-lock.txt
```

`doctor` resolves `sumo`/`netconvert` from explicit CLI paths first, otherwise PATH, verifies both report 1.24.0 and the Python clients match, and reports actionable missing-library errors. Unit tests must import/run without launching SUMO; integration tests require a successful doctor check. Do not silently substitute another simulator version. A deliberate version change updates the lock, compatibility metadata, and integration results.

### B. Generate the intersection and demand

`scenario build --output scenarios/single` writes new plain XML and invokes `netconvert` (do not hand-author its generated `.net.xml`). The [SUMO PlainXML format](https://sumo.dlr.de/docs/Networks/PlainXML.html) supports this input-to-network workflow.

Create these nodes: `J0=(0,0,type=traffic_light)`, `N=(0,250)`, `S=(0,-250)`, `W=(-250,0)`, `E=(250,0)`; external nodes use type `priority`. Create exactly four directed edges, each with two passenger lanes, speed 13.9m/s and priority 1:

| Edge | From → to | Role |
|---|---|---|
| `N_J` | N → J0 | N/S incoming lanes `N_J_0`, `N_J_1` |
| `J_S` | J0 → S | N/S outgoing lanes |
| `W_J` | W → J0 | W/E incoming lanes `W_J_0`, `W_J_1` |
| `J_E` | J0 → E | W/E outgoing lanes |

Connections XML lists only `N_J lane 0 → J_S lane 0`, `N_J lane 1 → J_S lane 1`, `W_J lane 0 → J_E lane 0`, and `W_J lane 1 → J_E lane 1`. No turns, reverse traffic, pedestrians, or imported map is required. Source XML generation is deterministic. Execute:

```sh
netconvert --node-files scenarios/single/nodes.nod.xml --edge-files scenarios/single/edges.edg.xml --connection-files scenarios/single/connections.con.xml --no-turnarounds true --output-file scenarios/single/network.net.xml
```

Validate exactly one signalized junction `J0`, the four desired movements, and positive incoming lane lengths. Junction geometry may shorten lanes from their coordinate length: capacity uses generated lane lengths, not an assumed 250m. Read [`trafficlight.getControlledLinks("J0")`](https://sumo.dlr.de/pydoc/traci/_trafficlight.html) at startup; derive each signal-character index from its incoming/outgoing lane pair and compare with the manifest. Never assume SUMO emits direction indices in a particular order. Build masks from that mapping. If indices happen to be NS,NS,WE,WE, the four masks are `GGrr`, `yyrr`, `rrGG`, `rryy`; these strings are examples, not unvalidated constants.

Create one vehicle type `car`: `vClass=passenger`, `length=5`, `minGap=2.5`, `maxSpeed=13.9`, `accel=2.6`, `decel=4.5`, `tau=1.0`, `sigma=0.5`, `carFollowModel=Krauss`, `speedFactor=1.0`, `speedDev=0`. Routes are `NS: N_J J_S` and `WE: W_J J_E`. Retain pinned SUMO defaults for unspecified dynamics and record the toolchain version.

Materialize individual vehicles in `.rou.xml`, not random runtime flows. Using a dedicated `random.Random(traffic_seed)`, for each integer second `t=0..H-1`, draw one uniform for NS then one for WE. Insert a scheduled departure when the draw is below that direction's probability. Assign incrementing vehicle IDs, per-direction alternating departure lane 0/1, `departPos=base`, and `departSpeed=0`; sort by departure time and NS-before-WE order. This generator is separate from controller and sensor-noise RNGs.

| Profile | NS probability per second | WE probability per second |
|---|---|---|
| balanced | 0.30 | 0.30 |
| asymmetric | 0.20 | 0.50 |
| peak | 0.15 before H/3; 0.45 until 2H/3; 0.20 afterward | Same as NS |

**Saturation check:** rough capacity arithmetic (about one vehicle per second of discharge for two lanes while green, shared between directions, minus yellow loss) suggests 0.45 per direction per second is near saturation and 0.55 would be oversaturated. Treat this as an estimate to verify, not a fact. During scenario smoke checks, run the queue controller on one peak seed and record the fraction of free-choice checks at which both direction scores are at their cap (2.0 within tie tolerance). If that fraction exceeds 25% of the peak window, lower the peak probability, record the change and reason in the scenario manifest, and regenerate. Some saturation is acceptable; a peak where both scores are pinned for most of the window makes the queue rule degenerate into retain-until-service-limit and gives Q-learning a single uninformative state.

Within each ordered train/development/final seed list, cycle profiles balanced → asymmetric → peak. Thus development seeds 100–102 cover all three; final seeds 200–204 cover balanced, asymmetric, peak, balanced, asymmetric. A resumed attempt uses `attempt_index % 3` and its stored seed allocation; aborted attempts consume an index. Persist each allocated index in its run manifest before traffic starts. On resume, the next unused index is greater than both the checkpoint allocation and every allocated run manifest in that experiment folder, including failed/abandoned runs. Restoring an older checkpoint never overwrites a later run or reuses its identity. If these required manifests are missing/corrupt, stop with a recovery error rather than silently reusing seeds. Record profile, seed, generator version, exact scheduled vehicles, XML hashes, and network hash in a scenario manifest. Reuse a given demand file byte-for-byte for paired policies. A demand seed produces planned arrivals, not a guarantee that congested vehicles can enter on time.

Start SUMO with generated network/route files, `--step-length 1`, `--begin 0`, `--end H+1`, `--seed <traffic_seed>`, and `--time-to-teleport -1`, `--collision.action warn`, and `--collision.check-junctions true`. The runtime closes at H without advancing to H+1. Disable teleportation to avoid silently removing congested vehicles; stop and report an incomplete run on any unexpected teleport/collision event, preserving its raw evidence. Use `sumo`, not `sumo-gui`, by default. The runtime controls signals via `setRedYellowGreenState`, which holds the state until changed; it must implement all yellow transitions itself ([SUMO state-control API](https://sumo.dlr.de/docs/TraCI/Change_Traffic_Lights_State.html)). No autonomous signal program or SUMO-RL scheduler may run in parallel.

### C. Default configuration and validation

Create this complete baseline `configs/default.yaml`; resolve relative paths against the project root and store a resolved copy in every run:

```yaml
schema_version: 1
simulation:
  horizon_s: 3600
  step_s: 1
  control_interval_s: 5
  tls_id: J0
  initial_green: NS
  gui: false
  sumo_version: "1.24.0"
network:
  directory: scenarios/single
  lanes:
    NS: [N_J_0, N_J_1]
    WE: [W_J_0, W_J_1]
signal:
  yellow_s: 2
  minimum_green_s: 5
  green_service_limit_s: 60
  all_red_s: 0
  fixed_green_target_s: 30            # pre-tuning default; must be < green_service_limit_s
  fixed_green_candidates_s: [15, 20, 25, 30, 35, 40, 45, 50, 55]
sensor:
  vehicle_length_m: 5.0
  minimum_gap_m: 2.5
  retries: 2
learning:
  alpha: 0.1
  discount_factor: 0.95
  discount_unit_s: 5
  epsilon_initial: 1.0
  epsilon_floor: 0.1
  epsilon_decay: 0.9995
  controller_seed: 0
  queue_edges: [0.0, 0.4, 0.8, 1.2, 1.6, 2.0]
  elapsed_edges_s: [0.0, 5.0, 32.5, 60.0]
  tie_abs_tolerance: 0.000000001
  evaluation_requires_both_actions_updated: true
storage:
  output_root: outputs
  checkpoint_every_updates: 100
  keep_previous: 1
  flush_each_tick: true
experiment:
  training_episodes: 15
  training_seed_start: 0
  development_seeds: [100, 101, 102]
  final_seeds: [200, 201, 202, 203, 204]
  development_after_episodes: [5, 10, 15]
robustness:
  levels:
    low:  {noise_bound_vehicles: 1, stale_probability: 0.05}
    high: {noise_bound_vehicles: 3, stale_probability: 0.20}
  noise_seed_offset: 10000
  saturation_check_max_fraction: 0.25
```

Reject unknown configuration keys, nonfinite values, invalid probability ranges, duplicate lane IDs, missing mappings, step size other than 1 for this version, nonpositive durations, `minimum_green >= green_service_limit`, nonzero all-red (unsupported here), any fixed-time target or candidate not strictly below `green_service_limit_s`, robustness levels with negative bounds or probabilities outside [0, 1], and queue/elapsed edges inconsistent with the selected two-lane state encoding. Require a positive integer horizon divisible by 5. Confirm installed simulator version and live mappings before traffic advances. Interpreting a checkpoint uses the compatibility rules below, not just successful JSON parsing.

### D. Typed component interfaces

Implement immutable records (dataclasses/enums) in `contracts.py`; JSON is a serialization format, not the internal module API. Direction is exactly `NS | WE`. Mode is `train | evaluate`. Signal phase is `NS_GREEN | NS_YELLOW | WE_GREEN | WE_YELLOW`; `current_green` is null during yellow, and `transition_target` is then required. Use integer simulated seconds and timezone-aware UTC strings only for wall-clock metadata.

| Record | Required fields |
|---|---|
| `RawSnapshot` | time, raw signal string, per-incoming-lane halting count, source time/status; independent raw reads for the safety monitor; lane lengths are cached validated metadata |
| `Observation` | ID, time, measured counts by lane/direction, raw queue scores, colors and color-start times by direction, phase, nullable current green, nullable elapsed green, nullable transition target, transition start; actual raw signal and measurement-quality metadata |
| `DecisionContext` | observation, allowed action set, operating-rule result, training/evaluation mode; Q-state exists only for eligible green observations |
| `Decision` | ID, observation ID, time, requested direction, source enum, reason enum, pre-selection values/counts, tie result, epsilon used where applicable, policy revision |
| `SubmissionResult` | decision/operating-event reference, time, target, status `accepted | blocked | failed | unconfirmed`, blocking/error reason; acceptance does not imply observed target green |
| `SignalEvent` | old/new raw masks and group colors, time, originating request/operating-event ID, transition target, monitor result |
| `PendingFeedback` | originating decision/state/action, start time, sample IDs, elapsed seconds, raw and discounted reward accumulators; not restored across episodes |
| `LearningUpdate` | decision reference, start/end, samples, original and next states, action, old Q, R, T, bootstrap values/mask, alpha/d, target, new Q, updated counts, policy revision |
| `RunResult` | completion/trace/persistence statuses independently, planned/observed duration, final valid timestamp, metrics, failure and checkpoint references |

Use these service boundaries; exact Python typing syntax may vary without changing ownership:

```text
SumoAdapter.start(config, scenario) -> initial raw state
SumoAdapter.read_raw() -> RawSnapshot                 # no time advancement
SumoAdapter.complete_due_transition(time) -> events   # uses monitored mask writes
SumoAdapter.request(target, origin, observation) -> SubmissionResult
SumoAdapter.advance_one_second() -> time              # called only by Runtime
SumoAdapter.close()
TrafficSensor.observe(raw, metadata) -> Observation
SafetyMonitor.check_proposed(mask, time, origin) -> allow/veto
SafetyMonitor.observe(raw, stage) -> violations
SafetyMonitor.check_service_deadline(observation, events) -> violations
Controller.select(DecisionContext) -> Decision        # no actuation or SUMO stepping
TrafficActuator.submit(Decision, Observation) -> SubmissionResult
Learner.close_interval(PendingFeedback, next_context) -> LearningUpdate
EventWriter.append(event); EventWriter.flush(); EventWriter.close()
CheckpointStore.save(snapshot); CheckpointStore.load(path, expected_contract)
Explainer.explain(run_directory, event_id) -> supported facts + readable text
```

Sensor retries repeat the raw retrieval transaction at the same time, up to three total attempts. Noise is injected once after successful retrieval, never redrawn to make a retry succeed. A read exception, invalid required field, unexpected mask, or nonmonotonic time is explicit failure. The only simulator writer is the adapter; all its low-level writes pass through the monitor and an immediate read-back. The monitor maintains its own history from raw read-backs and tick observations, not adapter timers. It does not independently advance SUMO.

### E. Exact runtime ordering and state machine

Initialization: start SUMO, validate links/lane lengths, set NS green through the monitor's one-time initialization exception, read back the mask, initialize green/color clocks to zero, obtain/log observation 0, and skip the policy check because minimum green is incomplete. Startup must not advance time to manufacture a previous observation. Run each later boundary at t=1..H as follows:

1. Advance exactly one second from the previous flushed boundary. Require reported simulation time to equal t. Read valid raw traffic/signal state (same-time retries allowed); monitor S1–S3 before accepting it. Unknown/unexpected signal changes stop the run.
2. If t < H and an accepted yellow transition has reached two elapsed seconds, propose the target green, monitor it, write it, and immediately read back. Record the actual signal change and update adapter and independently observed monitor histories. Do not execute this or any other command at H.
3. Construct one primary observation for t using the current, read-back-confirmed colors and current queue sample. Log it. Add its one-second reward once to an existing pending interval. In robustness evaluation, retain both true and measured counts but policy/reward use measured values. Do not sample or reward again for same-time mask read-backs.
4. At H: issue no new commands. If timing would allow a free choice and feedback is pending, close/update the interval in training; otherwise record it as discarded. A due forced switch or active yellow is not an eligible bootstrap boundary. Flush, save completed learning, close, and report statuses. No service-limit violation is invented solely because the experiment ended without issuing a new command.
5. Before H: enforce a reached service limit with measured competing demand immediately. Submit this as an operating event, not a controller choice. Observe/log its mask change; leave the original feedback pending. Check S4 after enforcement, using measured competing demand and independent green onset, with a deadline no later than the next observed second. In the noise experiment S4 checks the operating contract, not access to hidden truth.
6. If t is divisible by 5 and timing still allows a free choice, close/update previous feedback first. Select using the resulting Q-table revision. Log the frozen decision evidence, validate/submit, and observe any immediate yellow change. On acceptance, create the new pending interval and advance eligible-choice/epsilon progress in training; on a block or safety veto, do neither. New feedback begins with the sample at t+1, never t.
7. Append signal, learning, and error events in occurrence order; flush the tick before another advance. Process a due checkpoint at this boundary from a consistent complete snapshot, then flush its status event. Failure follows Module 6 rather than continuing to another tick.

State machine: retaining green sends no redundant mask write and preserves green onset. A switch from green writes outgoing-yellow/opposing-red at request time, stores one committed target, and reaches that target green exactly two seconds later. Only green onset resets the elapsed-green timer; color onset resets when that color actually changes. A newly green direction therefore needs five additional seconds before another optional switch, followed by waiting to the next global five-second check. The monitor permits valid startup but rejects direct opposing-green changes, premature yellow, mixed invalid masks, and missed forced deadlines.

All writes, including maintenance and forced switches, have origin IDs and monitor checks. A safety veto creates an explicit block with no new learning interval; repeated missed forced enforcement can subsequently trigger S4 and stop. Post-observation violations stop immediately and discard unfinished feedback. Two events at the same simulation second are ordered by event sequence number, not by timestamp alone. Signal bands are half-open intervals `[change_time, next_change_time)`.

### F. Event schema, reproducible policy history, and checkpoints

Each real JSONL line has an envelope: `schema_version:1`, `run_id`, unique `event_id`, monotonically increasing `seq`, integer `sim_time_s`, `type`, and `payload`. Earlier compact examples omit this envelope only for readability. Use UTF-8, one complete JSON object plus newline per append, strict finite JSON numbers (`allow_nan=False`), and explicit null for unavailable optional fields. Never pickle/eval records. Event types are `run_start`, `observation`, `decision`, `submission`, `signal_change`, `constraint_hold`, `operating_action`, `safety_override`, `safety_violation`, `learning_update`, `feedback_discarded`, `phantom_demand`, `masked_demand`, `checkpoint_saved`, `error`, and `run_end`. Include reference IDs and contract fields relevant to each type; operating/safety events need no fabricated decision ID.

At each run start write immutable `config.resolved.json`, `scenario.manifest.json`, `run.manifest.json`, and `policy_start.json` (full starting Q-table/counts, or null for baselines). `run_start` references their hashes. Each completed update increments `policy_revision` and logs old/new Q-values and counts with the originating state/action. Initial/missing entries mean zero with zero completed-update count. A decision references the revision after any previous-interval update and before its own selection. Offline explanations reconstruct that exact revision from `policy_start.json` plus ordered updates, validating old values. They must not use the final or mutable latest table for historical decisions.

Keep immutable copies `milestones/episode_005.json`, `episode_010.json`, and `episode_015.json` for development/final policy reports, in addition to rotating latest/previous recovery saves. Evaluation run manifests reference a frozen copy and its digest. If required history or a line is missing/malformed, mark the trace incomplete and the dependent explanation unavailable; never silently skip a bad line and claim a full replay.

Complete checkpoint top-level keys: `schema_version`, `checkpoint_id`, `training_id`, `created_at_utc`, `compatibility`, `progress`, `learning`, `controller_rng`, `q_table`, `source_run`, and `integrity_sha256`. `progress` includes completed episodes, episode-attempt allocation, accepted choices, completed updates, policy revision, and save generation. `learning` includes alpha, epsilon and decay/floor, and discount/time unit. `controller_rng` stores the Python random generator version and JSON-list representation of `Random.getstate()`; validate the shape and recursively restore tuples for `setstate()`, never unpickle. `q_table` is a list of unique four-component state keys with both finite action values and nonnegative completed-update counts; do not filter zeros. Store no pending interval as resumable learning.

Compute SHA-256 over canonical JSON (sorted keys, compact separators, UTF-8, finite values) excluding only the integrity field. Validate this digest, structure, states, counts, bounded epsilon, monotonic progress, RNG format, and compatibility before restoring. This detects accidental corruption, not authenticity against an attacker. Compatibility contains network/movement hash, normalized queue formula ID `halting_capacity_v1`, lane lengths/capacities, state bins/action order, timing/cadence/horizon, tie tolerance, alpha/discount semantics, exploration schedule, coverage rule, simulator/client/Python versions, and controller schema version. Paths, output names, requested number of additional episodes, and traffic seeds may differ; resume does not silently change learning semantics. Robustness evaluation may add its declared measurement wrapper but may not change state/reward encoding.

Write a checkpoint snapshot to a same-directory temporary file, flush and fsync it, close it, re-read and validate it, preserve the validated old latest into previous using its own temporary/replace operation, then `os.replace` the new file onto latest. Do not move away the only valid latest before publication or overwrite a valid backup with corrupt data. Attempt directory fsync on the reference Linux platform and report unsupported durability guarantees on other platforms. Treat any failed/uncertain publication as failure, as already specified; do not mark its counter saved early. Event-log flushing is per tick; fsync it at orderly shutdown, not after every observation. Power-loss durability still depends on the filesystem/storage stack.

### G. Offline explanation/report details

Provide `explain` for controller decisions and operating/safety event IDs. Each response includes measured vehicle counts, raw queue inputs, selection source and reason, alternative values with update counts, request versus actual execution, limitations, and later learning separately. Display `-Q` as **estimated discounted future congestion cost**, lower preferred, in queue-score-seconds at the one-second sampling resolution. Unvisited zero entries are labeled untrained, not zero expected congestion. When a direction's score equals its cap (each lane at estimated capacity), say so explicitly (“N/S and W/E are both at estimated lane capacity; queue scores cannot distinguish them, so the tie rule kept N/S green”). A faithful tie explanation must not hide that the tie came from saturation. Estimates are not guaranteed costs or confidence measures.

Global policy grids use a named frozen checkpoint. Plot 25 queue-bin cells for each green direction and elapsed category; mark minimum-green cells unavailable and bin-3 cells with nonzero competing queue category as constrained. A competing bin 0 can contain both exactly zero and positive demand, so mark bin-3/bin-0 cells conditionally eligible rather than pretending bins encode the exact constraint. Cells with missing action coverage show fallback. For comparison to the queue rule, unequal queue bins imply an ordering; equal-bin cells are marked ambiguous because raw values may differ. A midpoint illustration is permitted only if labeled as illustrative, not exact policy disagreement.

Counterfactuals are deterministic rule queries, not claims about physical interventions or random exploration. For an eligible exploitation decision with complete coverage, reconstruct its decision-time Q-table and search other queue-bin pairs with complete coverage in ascending Manhattan bin distance, then NS-bin and WE-bin lexicographic order. Hold actual green and exact elapsed time fixed. Exclude candidates forced by timing: at elapsed >=60, competing bins above zero are invalid and bin 0 requires an explicit zero-demand assumption. Report all changed bins and those assumptions. Explain that minimality means bin distance only, not fewest cars or a realizable traffic trajectory. If no supported candidate exists, state that. For exploration, fallback, timing holds, and safety events, show their actual reason and label a greedy-table counterfactual not applicable; never imply an exploratory choice was determined by argmax.

For queue-rule counterfactuals use raw comparison plus tolerance: with current green NS and fixed NS score q, switching to WE requires WE score strictly greater than `q + 1e-9`, subject to constraints; other current-green cases use the actual tie branch. Do not omit tolerance at a boundary. Verify reported alternatives by reevaluating the selection rule before rendering.

Generate `report.html` offline from immutable manifests, events, and required policy artifacts. Escape all dynamic content; embed local SVG/JS only and make every marker reference a unique event anchor. Include traffic/signal timelines, source counts, actual-versus-requested distinctions, per-decision detail, table grids where applicable, failures, and unavailable evidence. Baselines show no fake Q-table grid. Training reports show named start/end/milestone views rather than one final table falsely attributed to every earlier decision. Emit `summary.json` and `metrics.csv` with the same metric definitions as Module 8.

### H. Robustness experiment and output accounting

For each held-out seed and adaptive policy, run one clean evaluation and one evaluation per configured fault level (low, high) against the identical demand file and simulator seed. For each level, use a separate `random.Random(noise_seed_offset + traffic_seed)` wrapper, so both levels see the same draw sequence scaled to their parameters; iterate lanes in fixed order `N_J_0, N_J_1, W_J_0, W_J_1`. For each lane/tick after t=0, draw a uniform staleness value and an integer noise value uniformly from {-b..b} where b is the level's `noise_bound_vehicles`, always consuming both draws. With the level's `stale_probability` reuse the previous delivered measured count; otherwise deliver `max(0, true_count + noise)`. At t=0 deliver the true count. Recompute scores from delivered counts; record truth, measurement, and fault label. This models valid-looking faults for an explicitly labeled experiment, not sensor read-recovery behavior. Record `phantom_demand` and `masked_demand` diagnostic events (defined in Module 8) with true and measured competing counts; these are evaluation diagnostics, not safety violations.

Use true simulator counts for cross-policy congestion metrics and measured counts for policy/reward/explanation inputs. S4 checks measured competing demand; S1–S3 still check raw lights. Report actual scheduled, inserted, arrived, active, and not-yet-inserted vehicle IDs/counts so input blockage cannot look like low demand. Track departures and arrivals each second; classify every vehicle in the manifest exactly once at the horizon. Unexpected teleportation invalidates the normal accounting and must be reported. Reuse the same wrapper draw sequence per scenario across policies, while acknowledging that the traffic states it perturbs differ. Use `simulation.getDepartedIDList`, `getArrivedIDList`, `getStartingTeleportIDList`, `getCollidingVehiclesIDList`, and `vehicle.getIDList` for accounting; [SUMO simulation retrieval](https://sumo.dlr.de/docs/TraCI/Simulation_Value_Retrieval.html) distinguishes insertion from arrival. Do not use loaded-vehicle count as inserted demand.

### I. CLI and deliverables

Implement the following interface; run/experiment IDs are output identifiers printed by each command. Never overwrite an existing run directory. Treat below commands as usage acceptance criteria, not preexisting commands:

```sh
traffic-cps doctor
traffic-cps scenario build --output scenarios/single
traffic-cps scenario demand --network scenarios/single --seed 0 --profile balanced --horizon 60 --output scenarios/smoke
traffic-cps run --config configs/default.yaml --controller queue --scenario scenarios/smoke --horizon 60 --output outputs/smoke
traffic-cps train --config configs/default.yaml --episodes 15 --output outputs/training_001
traffic-cps train --config configs/default.yaml --resume outputs/training_001/checkpoints/training_latest.json --additional-episodes 5
traffic-cps tune-fixed --config configs/default.yaml --suite development --output outputs/fixed_tuning_001
traffic-cps evaluate --config configs/default.yaml --checkpoint outputs/training_001/milestones/episode_015.json --fixed-tuning outputs/fixed_tuning_001/fixed_tuning.json --suite final --output outputs/final_001
traffic-cps evaluate --config configs/default.yaml --checkpoint outputs/training_001/milestones/episode_015.json --suite robustness --output outputs/robustness_001
traffic-cps explain --run outputs/final_001/runs/RUN_ID --decision EVENT_ID
traffic-cps summarize --run outputs/final_001/runs/RUN_ID
traffic-cps report --run outputs/final_001/runs/RUN_ID
python -m pytest tests/unit
python -m pytest tests/integration
```

`train` generates required traffic files, writes an initial checkpoint before stepping, and retains milestones. It automatically evaluates immutable copies on development seeds at configured milestones using separate connections and RNGs; no concurrently shared SUMO connection is allowed. `tune-fixed` runs every fixed-time candidate on the development demand files and writes `fixed_tuning.json` with per-candidate metrics and the chosen target. `evaluate --suite final` requires `--fixed-tuning` and runs all three policies on the five final demand files; `--suite robustness` runs queue and frozen Q-learning clean, low-fault, and high-fault evaluations. `run` permits fixed/queue standalone baselines and optionally frozen Q-learning with an explicit checkpoint; training uses `train`. Each completed evaluation run generates its summary and HTML report; failures to generate required output are reported separately from simulation completion. Report commands never launch SUMO. Exit code 0 means the requested operation and mandatory persistence succeeded; 2 is invalid configuration/resume/environment; 1 is runtime, safety, or persistence failure. A completed simulation whose final save fails exits 1 with both statuses preserved.

Create manifests and initial policy artifacts before starting traffic. Preserve per-run SUMO stdout/stderr, event log, explanation data/report, and summary; keep experiment CSV/JSON aggregates outside individual run folders. Store hashes and requested overrides in the resolved configuration. A short smoke horizon never alters the production checkpoint's compatibility contract.

### J. Clean-room acceptance and known limits

The implementation is ready only when someone can copy this document into an empty project, create the specified files, install the pinned environment, generate network/demand, and execute the smoke path without importing or copying any XCPS asset. Validation includes:

- Network: exact movements and lane mapping, correct generated capacities, stable demand XML for a fixed seed.
- Timing: initial NS, 2s yellow, 5s minimum green, 60s competing-demand service, no conflicting colors; test the accepted 10s switch → 12s green → 20s next choice timeline with injected arrivals/state.
- Fixed-time attribution: with a target below the service limit, every fixed-time switch in a normal run is recorded and explained as `fixed_schedule`; configuration with target ≥ limit is rejected; tuning never reads final seeds.
- Scenario calibration: the peak saturation check runs and its result, plus any probability change, is recorded in the manifest.
- Robustness: phantom and masked demand events are detected against injected cases with known true/measured counts; both fault levels consume identical draw sequences per scenario.
- Runtime: 3,601 primary observations, 720 scheduled checks including skipped time zero, no command beyond or at the 3,600s cutoff; rewards counted once per second.
- Learning: known numerical interval update, unchanged original decision evidence, counts only on completed updates, correct epsilon progress, frozen coverage-aware evaluation, failure/cutoff discard behavior.
- Safety/faults: startup exception, every write checked, direct-green/premature-yellow/late-force injection, missing reads with same-time retries, valid-looking noisy counts kept separate from truth.
- Persistence: zero entries, RNG and counters round-trip; missing/incompatible/corrupt rejection; latest/previous preservation and selected recovery; interrupted writes and flush failures stop with accurate status.
- Explainability: historical Q-revision replay, supported counterfactuals, ambiguous-bin labels, raw-versus-measured distinctions, and report links that match event IDs.
- End-to-end: short runs of every controller, short training/save/restart/evaluation cycle, immutable evaluation checkpoint, then the full configured experiment suite.

This revision validates the specification and example syntax, not executable software. Actual installation, network generation, simulator behavior, numerical outcomes, storage durability, traffic improvement, and user-comprehension improvement must be checked during implementation. If an external dependency behaves differently, diagnose and version the compatibility change rather than silently rewriting the agreed semantics.
