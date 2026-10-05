# Traffic Controller: Interview Design Q&A

This document records agreed design decisions and concise explanations for software engineering interviews. These are planned behaviors for a standalone implementation in an empty repository; recording them here does not mean they have been implemented. The main design document contains the full build contract and does not require legacy XCPS code or assets.

## What is your failure mode when measurement retrieval fails?

**Interview answer:** “If a required sensor measurement cannot be retrieved, the system retries twice without advancing the simulation. It records the failure and notifies the user. If both retries fail, it ends the simulation cleanly and preserves the available results, clearly marking the run as incomplete. It never substitutes zero for a missing reading.”

### Agreed behavior

1. Attempt to retrieve the required measurements for the current simulation step.
2. On failure, record the affected measurement, step, and error, and issue a user-visible warning that retrieval is being retried.
3. Make at most two additional retrieval attempts: three attempts total. Do not advance simulation time or make a new controller decision during these retries.
4. If retrieval succeeds, record the recovery and continue the normal control loop using the valid observation.
5. If retrieval still fails, return an explicit failure to the runtime. Notify the user that the run is ending and identify the unavailable measurement.
6. End the simulation cleanly, close the simulator and log, and preserve completed records. Mark the run as incomplete and report completed steps and the failure location. Do not report a normal full-episode benchmark result.

### Why this choice?

Retries allow a temporary retrieval problem to recover. Ending the run after persistent failure prevents the controller from making a decision based on missing data. This is a simulation prototype, so a bounded retry policy followed by a clear stop is a manageable alternative to designing a separate degraded-operation controller.

Previously collected valid measurements remain usable, but any summary must identify that it covers only a partial run. Missing values are not zeros. This policy concerns required sensor retrieval; failures to save valid measurements follow Module 6's separate write-failure policy.

**Decision status:** Agreed. This replaces the earlier proposal to continue the simulation with a fallback signal action after persistent retrieval failure.

## What happens when both traffic directions have the same queue score?

**Interview answer:** “The queue-based controller keeps the current green when queue scores are equal, provided no higher-priority timing rule requires a change. If the configured green-service limit is reached while vehicles are waiting in the other direction, it requests that direction instead. It records which rule actually determined the request.”

This avoids switching solely because scores tie while still allowing competing demand to receive service. Requests are subject to minimum-green and transition rules. This is the specified tie behavior for the new controller; implementation is pending.

## Would Q-learning follow the same rules?

**Design explanation:** “The decision strategy changes, but the operating constraints remain. The queue-based policy compares congestion scores. Q-learning uses learned action values and sometimes explores. Both must respect signal timing and transitions. An explanation needs to identify whether a choice came from the policy or a timing constraint.”

Equal queue scores do not mean equal learned action values. Q-learning's approved tie behavior is recorded below. Its consolidated design is recorded in Module 2B of the main document.

## How does Q-learning break a tie between action values?

**Interview answer:** “When selecting by learned values, the controller keeps the currently green direction if it is permitted and tied for the highest Q-value. Random exploration is a separate selection mode and can still choose another permitted direction. Timing constraints always take priority.”

### Agreed behavior

- Apply this rule during exploitation, when choosing among highest-valued permitted actions.
- In the two-direction scenario, equal highest Q-values favor the currently green direction, provided that direction is permitted.
- If a timing rule requires another direction, follow the timing rule and explain that constraint rather than reporting a tie-based choice.
- During an existing yellow or clearance transition, complete the scheduled transition; do not use this tie rule to choose a new target midway.
- Record the Q-values, selected direction, and tie-breaking reason. Example explanation: “Both permitted actions had equal learned values, so the controller retained N/S green.”

**Decision status:** Agreed; implementation pending. Module 2B is consolidated. Start N/S green at time zero with elapsed green zero. Treat an absolute difference of at most 1e-9 as a tie, with no relative tolerance; bin unrounded values separately.

## Why separate Q-learning training from evaluation?

**Interview answer:** “Training allows exploration and updates the Q-table from experience. Evaluation loads a saved table, disables exploration, and freezes the learned values. This lets us inspect and explain the behavior of a particular learned policy without changing that policy during the benchmark.”

### Agreed behavior

| Mode | Exploration | Q-value updates | Purpose |
|---|---|---|---|
| Training | Enabled according to the configured exploration schedule | Enabled | Learn from simulation experience and save a Q-table. |
| Evaluation | Disabled; epsilon is zero | Disabled | Inspect and measure a saved policy on separate simulation runs. |

Both modes use current sensor observations, obey the same operating constraints, and record decision evidence. Evaluation retains the approved current-green tie rule. Freezing learning does not freeze traffic or signal actions. Evaluation must not modify the saved table; the approved queue-based fallback for unseen states is described below.

**Decision status:** Agreed; implementation pending. Queue and elapsed-time categories, reward, learning rate, discount factor, and the exploration schedule are agreed below. Signal timing defaults were finalized. The initial budget is 15 episodes; remaining integration choices are finalized in the main design.

## Is this a reproduction of the original project?

**Interview answer:** “This is a new standalone implementation. I generate the traffic network and demand and implement fixed-time, queue-based, and tabular Q-learning controllers in the same application. The original XCPS source and historical experiments are unavailable, so I do not claim to reproduce their results.”

**Decision status:** The standalone requirement supersedes the earlier v0 reproduction prerequisite. Only freshly implemented baselines and newly measured outcomes are required.

## What makes this a cyber-physical system?

**Interview answer:** “It's a simulated closed loop. SUMO plays the physical plant: vehicles and a signalized intersection. The sensor, controller, actuator, and an independent safety monitor are the cyber side, and the loop closes every simulated second. The project is about the parts that make CPS hard: timing constraints, a safety invariant that has to hold no matter what the controller wants, partial failures between command and execution, and sensing that can be wrong.”

Limits to state up front: time is simulated and discrete (no wall-clock deadlines); sensing is idealized by default because the sensor reads simulator ground truth, which real loop detectors can't measure; there is no all-red clearance interval. The baseline explicitly uses an idealized simulator-ground-truth sensor and adds a noisy-sensing evaluation. An E2 detector implementation is not required.

## How is signal safety guaranteed?

**Interview answer:** “The adapter enforces timing, but I don't trust the scheduler to check itself. A separate safety monitor reads raw signal states every second and checks four invariants: at most one direction is ever non-red, a green only follows the opposing yellow and red, no optional switch before minimum green, and a forced service switch starts within a second. It vetoes unsafe commands before submission and stops the run on any observed violation. This is an independent runtime assurance guard, not a claim of a complete, formally verified Simplex architecture. The validated time-zero initialization is the only exception to requiring a prior yellow transition.”

**Decision status:** Module 4A added. Fault injection tests prove it catches a deliberately broken adapter (green to green without yellow) and a controller that requests switches every second. Normal runs must report zero violations. Overrides are explanation sources, distinct from controller decisions and operating-rule actions.

## Does this project control one intersection or a network of intersections?

**Interview answer:** “The new application generates one signalized intersection with north-to-south and west-to-east traffic, using two incoming lanes per direction. We keep that scope to examine individual decisions and their evidence; multi-intersection coordination is outside this implementation.”

The network file defines one signalized junction, and the plant uses a single-agent environment. SUMO is the simulation platform; the single-intersection scope is a choice of this project's scenario and integration, not a claim about the platform's limits.

## Why use five queue bins, and what is the tradeoff?

**Interview answer:** “We group each direction's queue score into five categories so a tabular controller can reuse experience across similar observations. Five bins preserve more distinctions than three, but increase the number of state entries that need training. The boundaries are an approximation: two nearly identical scores on opposite sides of a boundary map to different entries. More bins give finer resolution, not automatically better decisions.”

### Agreed queue bins

For the current two incoming lanes per direction, each with a normalized lane queue value in [0, 1], use these bins over the summed direction score in [0, 2]:

| Bin ID | Score interval | Display label |
|---|---|---|
| 0 | 0 <= score < 0.4 | Very low |
| 1 | 0.4 <= score < 0.8 | Low |
| 2 | 0.8 <= score < 1.2 | Medium |
| 3 | 1.2 <= score < 1.6 | High |
| 4 | 1.6 <= score <= 2.0 | Very high |

These are project-defined categories, not standard traffic-engineering classifications. Use the unrounded score for binning; rounding for display must not change the category. Preserve the underlying score, vehicle counts, and resulting bin in decision evidence. An explanation should expose the category used without implying that crossing a boundary represents a sudden physical change in traffic.

For example, 0.399 and 0.400 are very similar measurements but map to bins 0 and 1. The Q-table does not automatically share updates between these neighboring bins. More training coverage may be needed, and validation should inspect behavior near boundaries as well as within bins. The five-bin choice does not by itself demonstrate improved accuracy or comprehension.

**Decision status:** Agreed; implementation pending. The generated two-lane direction score spans [0, 2]. Checkpoints with another state encoding must not be silently reused. These intervals apply to this two-lane-per-direction scenario; a different lane configuration requires revisiting the score range and bins.

## Why include elapsed green time in the Q-learning state?

**Interview answer:** “Two observations can have the same queue categories and current green direction but different timing contexts. Including elapsed green time lets the Q-table learn separate action values for an earlier versus a later decision window. The immediate reward still measures the resulting queues; timing affects which actions are permitted and the future outcomes those actions can lead to.”

### Agreed elapsed-time categories

Measure elapsed seconds from the start of the currently active green. Let `minimum_green` and `green_service_limit` be configurable durations, with `0 < minimum_green < green_service_limit`. Define `midpoint = (minimum_green + green_service_limit) / 2`.

| Bin ID | Interval | Meaning |
|---|---|---|
| 0 | 0 <= elapsed < minimum_green | Minimum green incomplete |
| 1 | minimum_green <= elapsed < midpoint | Earlier decision window |
| 2 | midpoint <= elapsed < green_service_limit | Later decision window |
| 3 | elapsed >= green_service_limit | Green-service limit reached |

Retain the exact elapsed time in the observation and explanation evidence. Timing constraints use exact measurements, not the bin labels. In bin 3, competing queued demand triggers service for the other direction; without competing demand, green can continue. Yellow and clearance intervals are handled separately and must not be counted as elapsed green time.

For example, `(medium, high, NS_green, earlier)` and `(medium, high, NS_green, later)` are distinct Q-table states. Each has its own action values. They can receive identical immediate rewards if the resulting queue scores match, and the learned values can also happen to be equal; elapsed-time binning permits different estimates without guaranteeing they differ.

**Reachability:** Free choices happen only after minimum green, so bin 0 never appears as a decision or bootstrap state. In bin 3, a choice exists only when the competing direction is empty, since otherwise the switch is forced. Coverage reports exclude unreachable combinations instead of reporting them as missing training.

The basic green-state encoding has `5 × 5 × 2 × 4 = 200` possible states. This counts combinations of queue categories, green direction, and elapsed-time category; it does not treat yellow or clearance as additional green phases. Timing bins remain an approximation: different exact times within one bin are grouped together.

**Decision status:** Four categories approved; implementation pending. Timing defaults finalized: minimum green 5s, yellow 2s, and green-service limit 60s. The midpoint is 32.5s, giving bins [0, 5), [5, 32.5), [32.5, 60), and [60, infinity). The earlier 10-second minimum was only illustrative. Transition and learning integration are finalized in the main design. The categories and thresholds must be recorded with the Q-table so changed state encodings are not silently reused.

## What happens when evaluation encounters an unseen state?

**Interview answer:** “If the current state has no recorded training visits, the system uses its queue-based controller for that decision. It reports that fallback explicitly instead of presenting initialized Q-values as learned preferences. Evaluation keeps the saved Q-table unchanged and reports how often fallback was used.”

### Agreed behavior

1. Apply the shared timing and transition constraints first. If they determine the action, report that constraint as the decision source.
2. When a choice is available, look up the state's recorded training visit count. An absent state or a state with zero training visits is unseen; do not infer experience from whether a Q-value is zero.
3. For an unseen state, use the approved queue-based selection rule over permitted actions. Retain the current green on equal queue scores when permitted.
4. Record the fallback source, unseen-state reason, queue comparison, observation reference, and selected action. Keep all learning disabled during evaluation, including training-count updates and insertion of new entries into the saved Q-table.
5. For a visited state, require at least one completed training update for each currently permitted action before exploiting the frozen table. Otherwise use queue-based fallback with reason incomplete_action_coverage. This conservative default was finalized during design closeout.

Save state and state-action training visit counts with Q-table checkpoints. Collect evaluation counters separately. Report fallback count and rate among decisions where the constraints allowed a choice; report constraint-determined decisions separately. Results describe the learned controller plus its fallback rather than implying that every decision came from Q-learning.

A visited state may still contain actions that were never tried. Preserve and expose per-action training counts so initialized estimates are distinguishable from estimates updated through experience. Fallback now also covers incomplete permitted-action coverage. Coverage counts represent completed valid training updates; selected-but-unfinished requests do not count as learned experience. One completed update still does not establish that an estimate is reliable.

**Decision status:** Explicit queue-based fallback approved; implementation pending. This differs from sensor retrieval failure: an unseen learning state has valid measurements, while persistent missing required measurements still ends the simulation.

## What does “learned policy” mean?

**Interview answer:** “A policy is the rule used to choose an action from a state. Our evaluation policy chooses a highest-valued permitted action from the saved Q-table and retains the current green on a tie. The selection rule is programmed, but the Q-values it uses were learned from training experience.”

The Q-table stores estimates; the policy uses them to choose. During training, epsilon-greedy behavior also includes random exploration. During evaluation, exploration is disabled. Timing constraints and the unseen-state queue-based fallback remain explicit decision sources, rather than being described as learned preferences.

## What reward and learning settings do you use?

**Interview answer:** “We accumulate the negative sum of queue scores sampled each second throughout an accepted action’s feedback interval. Lower queued congestion gives a higher reward. We use a learning rate of 0.1 and a discount factor of 0.95 per five simulated seconds, converting it to an equivalent per-second multiplier. These numeric defaults are specified in the new project configuration; discounting follows elapsed simulation time. These settings are not claimed optimal.”

| Setting | Approved initial value | Interpretation |
|---|---|---|
| Reward | `-(next_ns_queue_score + next_we_queue_score)` | Penalize remaining normalized queued congestion. |
| Learning rate, alpha | `0.1` | Move the previous state-action estimate 10% toward the update target. |
| Discount factor, gamma | `0.95` | Approved per five simulated seconds; use `0.95 ** (1/5)` per second and `0.95 ** (T/5)` for an interval lasting T seconds. Replaces the old per-learning-step semantics. |

Calculate each one-second reward from unrounded queue scores, not bin IDs or vehicle counts. It does not directly measure a reduction from the previous queue score or include a separate switching penalty. The approved runtime extension accumulates these rewards across an accepted action's feedback interval, so time spent congested contributes additional penalties. The single next-observation formula above is the per-sample building block, not the whole interval reward.

### Evidence for training updates

For each update, record:

- Previous state and action, with references linking them to the decision and observed outcome.
- Next state, resulting queue scores, and calculated reward.
- Old Q-value, the next-state action values used to form the target, permitted actions, and the resulting target.
- Learning rate, discount factor, and updated Q-value.

Keep decision explanations and learning explanations distinct. The decision explanation identifies why an action was selected. The learning explanation identifies how later feedback revised that action's estimate. Do not explain a choice using Q-values that were changed only after its outcome was observed.

Example learning explanation: “After requesting W/E green, the observed queue scores produced a reward of -1.0. The controller used this feedback to revise its estimate for the previous state and action.” This is illustrative, not an evaluation result or proof that the action alone caused the observed traffic.

Evaluation emits decision evidence but performs no learning updates. Module 5 specifies fixed-horizon cutoff and transition attribution. Natural early-terminal episodes are outside this version.

**Decision status:** Reward, initial alpha and gamma, and training-update evidence approved; implementation pending.

## Why keep a 10% exploration floor during later training?

**Interview answer:** “We retain a 10% exploration probability during later training so the controller continues trying alternative permitted actions. The other 90% of eligible choices use the highest-valued permitted actions and our tie rule. Evaluation disables exploration entirely so we can inspect the saved policy consistently.”

### Agreed setting

- Set the later-training minimum exploration probability, `epsilon_min`, to `0.10`.
- Apply exploration only when the operating constraints allow a choice. Timing-forced actions and ongoing transitions are not exploratory selections.
- Random exploration selects uniformly among permitted actions, including the action exploitation might also choose. A 10% exploration probability does not mean 10% of actions will differ from the greedy choice.
- Record the epsilon used for each eligible decision and whether exploration or exploitation actually selected it.
- Evaluation uses `epsilon = 0`, keeps learning disabled, and retains the approved queue-based fallback for unseen states.

The 10% floor provides more exploratory opportunities than 5%, but does not by itself guarantee improved coverage, performance, or explainability. Training visit counts and separate evaluation results are needed to assess the effect.

**Decision status:** The 10% later-training floor is approved; implementation pending. It replaces the previously proposed 5% floor. The approved initial exploration rate and decay rule are recorded below; the initial training budget is 15 episodes, with development evaluation after episodes 5, 10, and 15.

## How did you choose the exploration schedule?

**Interview answer:** “Our configuration starts at 100% exploration, multiplies it by 0.9995 after each accepted eligible training choice, and stops at a 10% floor. Forced actions and blocked submissions do not advance that counter. This reaches the floor after about 4,605 choices. That number follows from the settings; it is not proof that the controller has learned enough. We assess training experience, fallback usage, traffic performance, and explanation accuracy through separate evaluations.”

### Agreed schedule

- Begin a new training session with `epsilon = 1.0`.
- Use the current epsilon to select the action and record that value in the decision evidence.
- After each accepted eligible training request, apply `epsilon = max(0.10, epsilon * 0.9995)` and increment the eligible-decision counter.
- A step counts once when the controller can choose among permitted actions, even if it revisits a familiar state. Possible action branches are not counted separately. Timing-forced actions, ongoing transitions, and defensively blocked submissions do not advance the schedule.
- Preserve the Q-table, training visit counts, epsilon, and decision counter across episodes and in checkpoints. Resetting simulated traffic does not restart learning. Evaluation does not advance this schedule or overwrite the saved training epsilon.
- Keep the settings configurable and include them in run metadata.

| Eligible decisions completed | Approximate exploration probability |
|---|---|
| 0 | 100% |
| 500 | 78% |
| 1,000 | 61% |
| 2,000 | 37% |
| 3,000 | 22% |
| 4,605 and afterward | 10% |

The floor is first reached after `ceil(log(0.10) / log(0.9995)) = 4,605` eligible decisions. The standalone configuration runs 3,600 simulated seconds with a five-second control interval, or 720 control steps per episode. Even if every step allowed a choice, reaching the floor would require part of a seventh episode; actual timing restrictions generally increase that number. Measure eligible choices rather than assuming every control step counts.

These explicitly specified defaults provide a reproducible starting point, not experimental validation. They supersede the earlier discussion of a linear schedule over 5,000 choices.

### How will you know whether training is sufficient?

Save checkpoints and evaluate them with learning and exploration disabled. Check:

- **Experience:** Visits and completed learning updates for each state-action pair, especially both actions in frequently encountered states. A single visit does not demonstrate a reliable estimate.
- **Fallback usage:** How often evaluation encounters unseen states and uses the queue-based fallback. A low fallback rate does not by itself establish adequate training.
- **Traffic performance:** Waiting and congestion measurements compared with the queue-based controller under matched evaluation scenarios, repeated to assess variability.
- **Explanation accuracy:** Whether evidence and displayed reasons correctly identify exploration, learned-value selection, timing constraints, and fallback.

Reaching the exploration floor is not a training-completion condition. Do not report these checks as completed until they have actually been run. Use the 15-episode initial budget, development checks after episodes 5/10/15, and held-out final comparisons specified in Module 8. Stop at the configured budget and report coverage/performance limits rather than claiming convergence.

**Decision status:** Schedule and evaluation rationale approved; implementation and experimental validation pending.

## What does the controller return for explainability?

**Interview answer:** “Each decision returns a requested phase, a decision source, and the evidence used to select it. The explanation component formats those facts into readable text. We keep the requested phase separate from the actual signal behavior reported by the simulation.”

The supported movement set is fixed to N/S and W/E for this scenario. Do not repeat that fixed list as a user-facing 'permitted directions' field. Instead expose:

- Requested phase: N/S or W/E.
- Decision source: learned-value selection, exploration, queue-based fallback, or timing constraint.
- Evidence: the observation reference, state categories, pre-selection Q-values or queue scores, and any tie-breaking reason.
- Constraint reason when applicable: minimum green, service limit with competing demand, or transition in progress.

The internal action mask remains available for selection, learning, and diagnostics. Removing a redundant movement list from the display does not remove timing restrictions. Learning-update evidence is recorded separately after the outcome and linked back to the decision.

**Decision status:** Simplified decision record approved; implementation pending.

## How can Q-learning account for yellow without a yellow state in its table?

**Interview answer:** “The agent chooses a requested green direction. Yellow is part of executing a switch, not a separate action the agent chooses. We preserve the original decision while the transition runs, observe the congestion incurred during it, and use that feedback when updating the value of the original request. Yellow remains visible in the simulation trace even though the Q-table's decision states represent green phases.”

### Agreed behavior

- A phase-change request executes through the configured yellow and clearance sequence. No new selection or retargeting occurs during that transition.
- Record actual light colors, timestamps, and the originating decision reference throughout execution.
- Keep the queue-based congestion feedback incurred during the transition attributable to the request that initiated it; do not discard it or create a fictitious choice to 'take yellow.'
- Keep the planned 200-state green-state encoding. This is not a claim that the physical simulation has only 200 states or that every combination is reachable.

### Agreed normal interval and remaining learning integration

Keep the initiating state and accepted action pending until the next fixed five-second control point where a fresh policy choice is allowed. Collect congestion feedback every second throughout execution, including yellow, clearance, minimum green, and any wait until that eligible control point. In training, update the initiating state-action entry using the completed feedback and next decision-state estimates before selecting the next action. Continuing an existing green uses the same boundary. Becoming green or completing minimum green between scheduled checks does not itself trigger an update.

If these intervals can have different durations, both reward accumulation and future-value discounting must account for elapsed time consistently. Applying the old one-step formula unchanged after skipping an arbitrary number of simulation steps would not do this. This is an application of temporal abstraction, where an action can span several lower-level steps; see [Sutton, Precup, and Singh](https://www.sciencedirect.com/science/article/pii/S0004370299000521).

This extends the earlier next-observation reward description: the congestion formula stays the same at each sampling point, but samples across an execution interval contribute to its learning target. One-second observations, fixed five-second control points, the normal completion boundary, reward accumulation, forced-switch attribution, and planned experiment-cutoff handling are agreed. The discount time unit is approved as 0.95 per five simulated seconds. Natural early-terminal episodes are outside this fixed-duration version. Binned observations remain an approximation and do not establish a fully Markov state representation.

**Decision status:** Yellow as an enforced transition and its visibility/attribution are agreed. The normal feedback interval and reward accumulation were approved; implementation pending. The discount time unit was approved. Forced-switch and planned-cutoff rules are recorded below; natural early-terminal episodes are outside scope.

## In plain language, what gets updated after a yellow transition?

**Explanation:** “At State A, N/S is green and the controller observes the queues. It requests W/E green, normally because that action has a higher Q-value, or because it is exploring. The system remembers State A and that accepted request. N/S passes through yellow while W/E remains red, followed by any configured clearance interval. The system continues collecting congestion feedback. At the next scheduled control point where a fresh choice is allowed, it uses that feedback to update the value for requesting W/E in State A before making the next decision.”

The request is issued before the transition; it is not issued retrospectively when learning occurs. What changes afterward is `Q(State A, request W/E)`, not the historical observation or the earlier decision. Higher Q-values are preferred: for example, -2 is higher than -5. Explanations show readers the negated value as estimated discounted future congestion cost: 2 versus 5, lower is better. A transition can span multiple simulation steps rather than exactly one yellow step.

The update refines the estimate used when State A is encountered again. One-second sampling, reward accumulation, the normal update boundary, and the discount time unit are agreed. Forced-switch and planned-cutoff rules are recorded below; natural early-terminal episodes are outside scope. Evaluation records the transition and outcome without updating Q-values.

For example, with the finalized two-second yellow and five-second minimum green, a switch accepted at 10s reaches green at 12s. Skip the 15s check, observe minimum green finishing at 17s, and complete the interval at the next eligible check at 20s. The three-second scheduling delay remains part of the observed feedback. These durations are the finalized simulation defaults; the timeline remains an illustrative example. This normal example assumes no intervening forced action or episode ending.

## How does accumulated reward fit into the Q-learning equation?

**Interview answer:** “We replace the single-step reward with accumulated congestion feedback over the accepted action's interval. Then we add the discounted estimate of what can happen after that interval. We move the original state-action value 10% toward that combined target.”

Illustrative training example, not measured results: at 10s in state A, accept a request for N/S green with `Q(A, NS) = -15`. The next eligible control point is 20s in state B. The ten per-second rewards observed at 11s through 20s are `[-1.4, -1.2, -1.0, -0.8, -0.6, -0.6, -0.5, -0.5, -0.4, -0.4]`. Their raw sum is `-7.4`.

Using the approved discount of `0.95` per five seconds, the per-second multiplier is `d = 0.95 ** (1/5)`, approximately `0.989794`. Accumulate `R = r_1 + d*r_2 + ... + d**9*r_10`, approximately `-7.1578`. If the existing state-B values are `Q(B, NS) = -6` and `Q(B, WE) = -9`, and both actions are allowed, `next_best_Q = -6`. This value comes from the saved table's experience, not from executing another future action. It estimates return starting at B, beyond the observed interval. It can be inaccurate; unvisited entries initially hold zero under the training initialization rule.

The interval lasts ten seconds, so its future multiplier is `d**10 = 0.95**2 = 0.9025`. The target is `-7.1578 + 0.9025*(-6) = -12.5728`. With alpha `0.1`, update `Q(A, NS)` to `-15 + 0.1*(-12.5728 - (-15)) = -14.7573`. State B's entries are read to form the target; this update writes the original state-A/action entry. If states map to the same table entry, read the bootstrap values before writing the update.

The old estimate becomes less negative because the target is better than the old estimate, even though all observed rewards are negative. The next actual action is selected afterward and may explore; Q-learning's target still uses the highest allowed next-state value. The current reward interval and the next-state estimate cover successive periods, preventing double-counting of the observed congestion.

**Decision status:** Accumulation approved. The discount of 0.95 per five simulated seconds was approved; implementation pending. Forced actions and episode-end boundaries are outside this normal example.

## How does learning handle a forced switch?

**Interview answer:** “The adapter enforces a required switch when the green-service limit is reached with competing queued demand. It does not wait for a policy check. We label the switch as an operating-rule action, keep collecting feedback for any pending controller decision, and update that original decision at the next scheduled point where a fresh choice is allowed. This teaches the controller about outcomes within the system's operating rules without pretending it chose the forced action.”

- Preserve the original pending state/action and all rewards already collected. Include feedback during the forced transition, required green, and remaining wait until the next eligible scheduled choice.
- Record the forced reason and actual transition separately; no new controller decision, exploration-decay step, or independent learning interval is created by the forced switch.
- Do not update merely at the forced-switch event or during yellow. At the next eligible scheduled choice, update the original entry before selecting the next action in training. Evaluation never updates Q-values.
- If no controller action is pending, record the event without fabricating an action to learn from.
- The update reflects behavior under the combined controller and operating rules; it is not evidence that the original choice alone caused every subsequent traffic outcome.
- Preserve the existing failure-stop policy. Planned episode cutoffs follow the agreed rule below.

**Decision status:** Forced-switch handling approved; implementation pending. The discount time unit and numeric signal defaults were finalized.

## What happens to learning when a run reaches its planned end?

**Interview answer:** “At the experiment cutoff, we collect the final observation and issue no new action. If the pending decision has reached a valid feedback boundary, we update it using the observed reward and the final state's estimated future value. Otherwise, we retain its trace but skip that unfinished update. We preserve all earlier completed learning and save the run.”

At 3,600 seconds, a final update is permitted only when the timing rules would allow a fresh policy choice. Yellow, minimum green, or a mandatory operating action prevents that boundary from completing. Do not extend the run to finish the interval. A cutoff outside the fixed scheduling grid likewise does not introduce an extra update boundary. Evaluation performs no learning updates.

The horizon is a time limit on the experiment, not proof that future congestion disappears. Retain the next-state estimate in a valid final update rather than forcing it to zero. A successful run remains complete even if its last learning interval is unfinished; a sensor or execution failure marks the run incomplete. Clear pending feedback before resetting traffic, while retaining the table and exploration progress. Persistence failures must be reported separately.

**Decision status:** Planned experiment-cutoff handling approved; implementation pending. True terminal scenarios remain separate from this fixed-duration design.

## Does the Q-table start from scratch for each simulation run?

**Explanation:** “No. Within one training experiment, each episode resets traffic while retaining the Q-table, training counts, exploration probability, and decision counter. A fresh table is needed for the first training session or an intentionally new model. If the state/action encoding or reward meaning changes incompatibly, the old table cannot simply be reused.”

New state-action entries start at zero under the training initialization rule; discovering one does not reset the rest of the table. Evaluation loads a frozen table and uses the agreed fallback for unseen states without inserting training entries. Completed valid updates survive an interrupted episode if checkpoint persistence succeeds. Missing, corrupt, or incompatible checkpoints must not be described as successfully resumed learning; recovery behavior will be finalized in the checkpoint module.

## What is the actuator responsible for?

**Interview answer:** “The actuator receives a controller's phase request, validates it, translates it to the simulator's phase identifier, and submits it through the simulation adapter. It reports submission success or failure. The adapter separately reports the actual signal state, so we distinguish a command being submitted from a light actually becoming green.”

Each submission retains the originating decision reference, allowing the explanation to connect the requested direction with later signal observations. Timing and transition execution belong to the adapter; the actuator does not choose a policy or independently advance the simulation.

## What happens if the controller sends an invalid or missing phase request?

**Interview answer:** “The actuator rejects it, reports the error, and asks the runtime to stop the run cleanly. We preserve the available records and mark the run incomplete. We do not silently turn an invalid request into a different valid phase, because that would hide a bug and could make the explanation disagree with the executed action.”

This replaces the existing actuator's phase-zero default and modulo remapping. Invalid commands are not retried under the sensor retrieval policy. Simulator command failure and uncertain-delivery behavior are recorded below.

**Decision status:** Actuator boundary and invalid-request behavior approved; implementation pending.

## What happens if a valid phase request is blocked by signal timing?

**Interview answer:** “The simulation adapter preserves the currently required signal behavior and reports the timing restriction. It does not queue the blocked request for later execution. When a choice becomes available, the controller makes a fresh decision using current traffic observations.”

### Agreed behavior

- The adapter is the authoritative source of signal timing and transition state. Controllers use that information rather than maintaining independent transition timers.
- If a switch request arrives before minimum green completes, retain the current green and return a timing-blocked result with the originating decision reference, timestamp, and reason.
- If a conflicting request arrives during yellow or clearance, complete the already committed transition. Do not change its target or queue the new request.
- Record the blocked request for diagnostics and explanation, but do not describe it as an executed switch or a pending command that will automatically run later.
- At a subsequent eligible control point, obtain current observations and select a fresh action. Passage of time alone does not activate a discarded request.
- Distinguish successful submission, a known timing block, an invalid request, and a simulator/communication error. A timing block alone does not terminate the simulation; an invalid or missing request follows the approved stop policy.

For example: “W/E green was requested, but N/S minimum green had not elapsed. The request was not scheduled; the controller will reconsider using fresh observations.”

The runtime must preserve the distinction between a blocked request and an action actually carried out when assigning learning feedback. A defensively blocked new request creates no learning interval and does not decay epsilon or increment accepted-choice progress. Keep any previous interval update already completed before selection. Observations with no pending accepted action remain in the trace without being assigned a fictitious learning action; retry with a fresh choice at the next eligible scheduled check. Simulator command failures and uncertain delivery are separate cases, not timing blocks; they follow the stop policy below.

**Decision status:** Timing-block behavior approved; implementation pending. The adapter boundary and failure behavior are now consolidated in the main design document. Numeric signal defaults were finalized; runtime integration details are finalized in the main design.

## What happens if a simulator command fails or its delivery is uncertain?

**Interview answer:** “We stop issuing commands and end the run cleanly. We do not blindly retry because the command may already have taken effect. The adapter reports the failure or uncertainty, and the runtime preserves records, marks the run incomplete, and closes the simulator.”

This differs from a known timing block, which leaves execution state understood and permits the simulation to continue. When delivery is uncertain, record the last verified signal state and the unconfirmed command separately; do not claim that the command definitely failed to execute or that the simulator stopped at a precisely verified state.

## If a run stops early, is the data and Q-table learning still usable?

**Interview answer:** “We keep Q-table updates supported by completed, valid action outcomes. If the latest action has an incomplete or uncertain outcome, we preserve its diagnostic records but do not learn from that partial feedback. On clean shutdown, we save the last valid learning state so the next training episode can start with fresh traffic and retain the completed learning.”

### Agreed shutdown and partial-learning behavior

1. Persistent required sensor failure stops the run after the initial attempt and two retries at the same simulation time. An invalid request, simulator command failure, or uncertain command delivery stops the run without retrying the command.
2. The runtime stops advancing the simulation and issuing new commands. It records the cause, failure location, and last fully completed step. A failure during step 500 of a planned 720-step run does not make step 500 complete.
3. Retain earlier Q-table updates based on valid, completed outcomes. Do not update from missing measurements, uncertain execution, or an unfinished feedback interval.
4. Do not treat an error interruption as a normal terminal outcome or invent a final reward.
5. During clean shutdown, save the last valid Q-table, completed-update counts, training experience metadata, and exploration progress. Clear any pending state/action and unfinished reward accumulation before the next episode; do not connect an old action to newly reset traffic.
6. Preserve valid observations and decisions. Label failed or uncertain records explicitly, and label traffic summaries as partial rather than reporting a completed-episode benchmark.
7. During evaluation, preserve partial evaluation records without changing the saved Q-table or training progress.

These guarantees concern a controlled shutdown where persistence succeeds. Abrupt process termination or a failed checkpoint write requires separate persistence handling in the logging/checkpoint module; do not claim unsaved in-memory updates survived. Previously completed learning remains usable, but is not thereby proven accurate or sufficient.

**Decision status:** Command-failure stop policy, clean-shutdown behavior, and retention of valid partial-run learning approved; implementation pending.

## Does a one-second simulation tick mean the controller chooses every second?

**Explanation:** “No. A simulation tick advances traffic and provides observations. A scheduled control point checks whether a controller decision is appropriate. A learning update revises a Q-value only after a valid feedback interval is complete. These are different events.”

Under the agreed one-second observation interval and fixed five-second control interval:

| Episode duration | One-second simulation advances | Scheduled five-second control points |
|---|---|---|
| 720 simulated seconds | 720 | 144 |
| 3,600 simulated seconds, the existing episode duration | 3,600 | 720 |

These counts assume control points at time zero and every five seconds strictly before the episode ends. The final horizon observation completes the run rather than issuing an extra action. Timing restrictions can reduce free decision opportunities below the scheduled count. Repeated visits can update the same Q-table entry, and evaluation performs no learning updates.

**Status:** One-second observation intervals and fixed five-second control points approved; implementation pending.

## Why consider a decision before minimum green has elapsed?

**Explanation:** “We do not need to invoke the policy when timing already determines the behavior. The runtime should continue observing and collecting feedback, but skip policy selection while minimum green is incomplete or a transition is underway. It records the timing reason without counting a free choice or advancing exploration decay.”

The standalone configuration contains separate five-second settings: `control_interval_s = 5` sets the policy-check cadence, while `minimum_green_s = 5` sets the minimum green duration. Equal numeric values do not make them the same concept. The rebuild must measure actual elapsed green time from when green begins, excluding yellow.

A fixed five-second control interval was selected because it preserves the existing experiment cadence and bounds how often the policy reconsiders. It is a configurable starting choice, not a proven optimum or a universal real-world minimum. Fixed scheduling can delay the next free decision until the next control point after minimum green expires; that tradeoff is accepted for this version. The schedule does not reset after a decision or switch to an eligibility-triggered policy call.

Real minimum-green settings depend on the movement and intersection, including driver response, detector placement, queued vehicles, and pedestrian needs. See [FHWA Traffic Signal Timing Manual, Chapter 5](https://ops.fhwa.dot.gov/publications/fhwahop08024/chapter5.htm). Existing prototype settings are not evidence of road-deployment suitability.

The adapter's ability to block an unexpectedly early request remains a defensive check. Routine minimum-green holds should be detected before asking the controller to generate a request that cannot execute. Mandatory timing behavior remains enforced independently of optional policy reconsideration.

**Status:** Fixed scheduling with timing-based skipping approved; implementation pending. Signal durations, normal feedback boundaries, and planned-cutoff handling are now finalized; integration details are finalized in the main design.

## Is a blocked decision queued until it becomes executable?

**Interview answer:** “No. We log and discard a timing-blocked request. At the next fixed five-second control point, the system checks timing again and, if a choice is available, requests a fresh decision using current observations. An accepted switch is different: its transition target stays committed while yellow and clearance execute.”

The runtime also remembers a previous state and accepted action while collecting learning feedback. That memory exists to update the appropriate Q-value, not to execute the action again. A low-level submission buffer likewise does not grant permission to retain a timing-blocked request until its restriction expires.

## Does the five-second decision interval have to match yellow duration?

**Interview answer:** “No. The observation interval determines how often we inspect the traffic and light state. The control interval determines when we check for another policy decision. Yellow duration determines how long part of the signal transition lasts. One-second observations can capture a two- or three-second yellow even with control points five seconds apart.”

A scheduled check during yellow produces a timing explanation and continues observation, rather than a new controller choice or Q-table update. The runtime keeps collecting valid feedback for the accepted action. A decision opportunity can retain the current green; it does not necessarily cause a switch.

**Decision status:** These request and interval distinctions are part of the approved runtime schedule; implementation pending.


## What signal durations did you choose, and why?

**Interview answer:** “We specify two seconds of yellow, five seconds of minimum green, and a 60-second green-service limit as configurable simulation defaults. These keep the initial experiment manageable while we focus on decision evidence and learning integration. We measure green time from when the light actually becomes green and enforce timing independently of the five-second policy schedule. These defaults are not claimed optimal.”

The outgoing direction becomes yellow for two seconds while the opposing direction remains red, then the opposing direction becomes green. The newly generated network and adapter use no additional all-red phase in this version. Yellow does not count toward the new direction's minimum green. Retaining green does not restart its timer.

At or beyond 60 seconds of actual green, competing queued demand forces service for the other direction. Without competing queued vehicles, green can continue. If demand arrives later, the adapter enforces the rule when observed on the one-second simulation cadence, without waiting for a scheduled policy check. The service clock runs from green onset, not from when conflicting demand first arrives.

The minimum-green setting and five-second controller interval are separate parameters. A green beginning at 12s satisfies minimum green at 17s, but the next optional controller decision occurs at 20s. The elapsed-green bins are [0, 5), [5, 32.5), [32.5, 60), and [60, infinity); exact elapsed time still controls operating rules.

**Decision status:** Simulation timing defaults finalized; implementation pending. See Module 2A of the main design document.

## What are the program, training, a run, and a checkpoint?

The program is the Python application coordinating SUMO and the CPS components. One invocation can execute many episodes. A run or episode is one traffic simulation, currently 3,600 simulated seconds with 3,600 one-second advances and 720 scheduled control checks. Training means updating the Q-learning controller's action estimates from simulation feedback; SUMO and the sensors are not being trained. The Q-table holds those estimates in program memory. A checkpoint is a file saving that table and supporting progress so learning survives closing the application.

## How does a checkpoint actually restore training?

**Interview answer:** “We read a compatible checkpoint and rebuild the controller's Q-table in memory, along with visit counts, exploration probability, and the decision counter. Then we start a fresh traffic episode. The controller uses its restored scores for new decisions and continues updating them from new outcomes. We restore learned progress, not a paused traffic scene.”

For example, if a successfully saved checkpoint contains `Q(A, NS) = -14.7573` and exploration has reached its 10% floor, loading it restores that value and exploration setting. The controller does not restart with a zero-valued table and 100% exploration. It can apply the saved entry whenever it observes state A in a new episode. The checkpoint must preserve the matching decision counter and configuration as well.

The event log and checkpoint serve different purposes. Each run gets a separate JSONL history; each line records an observation or event. The JSON checkpoint saves current learned state and training metadata. The controller reads the in-memory table during decisions; loading the checkpoint happens when restoring progress, not for every request. We do not retrain by replaying the log under this design.

Only successfully saved progress is recoverable from the checkpoint. Unfinished feedback is cleared before the new episode. Restore the saved controller random-generator state, but do not claim this reproduces vehicle positions or the interrupted traffic trajectory. Verify state bins, action mapping, reward/discount semantics, and other compatibility metadata before using saved values. The approved save cadence and checkpoint recovery rules are recorded below and in Module 6.

**Decision status:** Separate per-run JSONL logs and JSON training checkpoints, with the example structures and roles discussed, approved; implementation pending.

## When do you save checkpoints, and how do you protect the previous save?

**Interview answer:** “We record each learning update in the event log and save the complete training state after every 100 completed Q-table updates, at episode end, and during controlled shutdown. We write a temporary file first, then atomically replace the latest checkpoint only after the write succeeds. This avoids rewriting the checkpoint for every update while protecting the previous save from a partially written replacement.”

Count updates since the last successful save, not controller checks or newly discovered states. Save all current entries, including zeros and visit counts. Take a consistent snapshot so the table and supporting progress agree. Evaluation does not change the training checkpoint.

With successful scheduled saves, fewer than 100 additional updates normally accumulate between saves. This is not a strict 99-update crash-loss guarantee: a crash during the due save can lose 100, and failed saves can increase the gap. Atomic replacement protects file publication; detailed disk durability and failure response still need implementation. An unfinished feedback interval is never converted into a completed update just to save it.

**Decision status:** Checkpoint cadence and temporary-file replacement approved; implementation pending. The agreed write-failure policy follows below.

## What happens if an event log or checkpoint cannot be saved?

**Interview answer:** “We stop the simulation cleanly rather than silently continue without reliable records. If the event log fails, we notify through application output and attempt to save completed learning to a checkpoint. If checkpoint saving fails, we preserve the previous valid checkpoint and report that newer progress was not saved. We distinguish simulation completion from storage success.”

Do not blindly retry a failed event write, because part of the record may already have been written. Do not repeatedly retry a failed checkpoint during shutdown or report a logging error exclusively through the broken logger. Write an error to the event log only if that path still works. The sensor's two retrieval retries do not apply to persistence failures.

Keep previous valid records and completed learning; exclude unfinished feedback from Q-value updates. A successful emergency checkpoint preserves learned state but does not repair a missing event trace. If no checkpoint was ever saved, say so. If save completion is uncertain, report it as unconfirmed. Only successfully saved progress can be promised on restart.

Example when an earlier checkpoint is confirmed intact: “Checkpoint saving failed. Simulation stopped at 1,245 seconds. Your previous checkpoint is intact; learning since that checkpoint was not saved.” If failure occurs after reaching the planned horizon, report “simulation completed, saving failed” instead.

**Decision status:** Log/checkpoint write-failure handling approved; implementation pending. The agreed checkpoint-loading policy follows below.

## How do you handle checkpoint recovery, flushing, and reproducibility?

- **Missing/corrupted/incompatible checkpoint:** Stop before starting traffic, explain the problem, and never silently reset learning. Explicit fresh training uses a separate folder.
- **Checkpoint backups:** Keep the latest and one previous valid save. Validate a new snapshot before rotation and preserve a valid recovery copy throughout replacement. Explicitly select an older compatible save for recovery; log its identity.
- **Event-log flushing:** Flush after every simulated second before advancing again, and during shutdown. Flushing failures follow the stop policy. Flushing application buffers is not a guarantee against power loss.
- **Randomness:** Save and restore the controller's random-generator state alongside learning progress. Record each episode's traffic seed and configuration.
- **Resume behavior:** Restore saved learning and controller randomness, then start fresh traffic. This is not a paused simulation restore or a promise of identical future trajectories.

**Decision status:** Approved. Logging/checkpoint module design complete for the agreed scope; implementation and validation pending.

## Which explainability principles guide the explanation layer?

**Design answer:** “We map our explanation layer to NIST's principles of explanation, meaningfulness, explanation accuracy, and knowledge limits. Our engineering choices are linked evidence, readable text, vehicle counts, actual decision reasons, execution status, separate learning details, and explicit limitations. The principles justify these choices; they do not prescribe that exact list.”

See NIST's [Four Principles of Explainable Artificial Intelligence](https://nvlpubs.nist.gov/nistpubs/ir/2021/NIST.IR.8312.pdf). This mapping was developed after the initial component proposal. Do not claim that the standard mandated our architecture or that mapping to it establishes validated compliance.

- **Input evidence:** Ground claims in linked records of what the system observed and did.
- **Readable output:** Translate recorded evidence using plain-language templates.
- **Vehicle counts:** Give familiar congestion context alongside the scores actually used.
- **Decision reason:** Faithfully distinguish Q-value selection, queue-based selection, exploration, fallback, ties, and operating rules.
- **Execution status:** Separate the requested action from submitted commands and observed light changes.
- **Learning details:** Explain later updates separately from the earlier decision, using the appropriate values at each time.
- **Limitation indicator:** Identify unseen states, missing records, and unconfirmed outcomes instead of guessing or claiming unsupported confidence.

For example, randomly selecting N/S must be explained as exploration even if N/S has more queued vehicles. Otherwise, the explanation sounds plausible but misrepresents the decision process. Linked records provide traceability from observation to choice to actual execution.

A higher Q-value explains why the selection rule preferred an action; it does not fully explain how that value arose. Separate update records help inspect its learning history. The design does not yet demonstrate improved user comprehension; that requires implementation and evaluation. Use design-tense interview claims until the relevant behavior has been implemented and checked.

### Intended reader, approach, and terminology

- **Reader:** a traffic or controls engineer inspecting and debugging the controller. NIST's meaningfulness principle depends on audience, so the templates are written for that person.
- **Interpretable by design:** both policies are directly inspectable (a comparison and a 200-state lookup table), so explanations read the actual decision rule rather than approximating it after the fact (Rudin, 2019). This is the central explainability claim, and it holds only while the policy stays tabular.
- **Terminology:** Q-values are presented as estimated discounted future congestion cost (negated, lower is better), with raw values in evidence. "Score" means only the sensor's queue score. Retain decisions get explanations too, since they're most of the decisions.

**Decision status:** Explanation components, principle mapping, template-based reasons, execution distinctions, and limitation reporting approved; implementation and evaluation pending.


## What training and evaluation setup is planned?

**Design answer:** “We start with 15 one-hour simulated training episodes, evaluate frozen checkpoints after episodes 5, 10, and 15, and compare the final checkpoint against fixed-time and queue-based controllers on five held-out traffic scenarios. Every policy receives the same generated departure/route plans and shared operating constraints. We report per-scenario results and variability rather than assuming one run demonstrates improvement.”

Training traffic seeds are 0–14, development seeds 100–102, and final comparison seeds 200–204. Controller randomness is separately seeded and checkpointed. The initial final model is the episode-15 checkpoint; do not select it using final-test outcomes. Additional tuning uses development scenarios and fresh final scenarios when needed. Fifteen episodes is an initial budget, not a convergence guarantee.

Primary traffic metric: mean queued vehicles from one-second samples over the full horizon. Also report peak queue, accumulated queue exposure, completed/remaining/uninserted demand, switches, fallback, training coverage, and wall-clock time. Show individual results, means, sample standard deviations, and paired baseline differences. Report failed runs separately. The old prototype's waiting-time reduction is not evidence of this rebuild's results.

Additions from design review:

- **Standalone baselines:** implement fixed-time and queue-based controllers alongside Q-learning; no original code or reproduction run is required.
- **Demand variety:** scenarios include balanced, asymmetric, and peak demand, recorded per seed.
- **Q-only versus fallback:** report what share of frozen-evaluation choices came from the table versus the queue-rule fallback. If fallback dominates, the comparison mostly measures the queue rule.
- **Sensing robustness:** an evaluation-only wrapper adds bounded noise and stale readings; report degradation and show what the controller saw versus the truth.
- **Unfavorable result:** exploration reaches its floor after 4,605 accepted choices, so the time spent above the exploration floor depends on accepted decisions per episode. If Q-learning loses to the queue rule, report it with coverage and policy-disagreement evidence rather than tuning on the final scenarios.

## How do you explain the whole policy, not just one decision?

**Interview answer:** “Per-decision explanations are local. Because the policy is a table, I can show all of it: for each current green and timing window, a 5 by 5 grid of N/S versus W/E queue level showing the preferred action, the cost gap, and how much training backs it. Where queue-bin ordering supports a comparison, disagreements with the queue rule help identify behavior worth inspecting; equal-bin cells are ambiguous because raw queue values can differ. For covered exploitation decisions I also show a policy-level counterfactual: the nearest supported queue-bin state with a different greedy action, including its assumptions.”

- **Global view:** generated per checkpoint (episodes 5, 10, 15) so readers can watch the policy change; fallback and unreachable cells are marked.
- **Contrastive:** people ask “why this rather than that” (Miller, 2019). Each explanation states the alternative and its value. The counterfactual searches neighboring queue bins in the decision-time table, for example “Would have requested W/E if its queue had been High instead of Medium.” It describes the rule, not traffic causation.
- **Optional reward decomposition:** the reward already sums N/S and W/E components, so separate component tables could explain “saves 6 units of N/S congestion, costs 3 of W/E” (Juozapaitis et al., 2019). This doubles the table and changes the checkpoint, so it comes after the baseline works.

## How can someone actually see the explanations?

**Interview answer:** “Nobody should have to read JSONL. There's an `explain` command for any decision and a `summarize` command per run, plus a static HTML report: a timeline of waiting vehicles and signal colors with decision markers colored by source, each linking to its explanation, and the global policy view. An hour has 720 scheduled checks and fewer actual policy decisions, so the episode summary points to the notable ones: switches, forced actions, fallbacks, exploration, and disagreements with the queue rule.”

The report is generated offline from the event log and checkpoint; a live dashboard stays out of scope. Implementation order builds a thin end-to-end slice (queue policy, event log, explanation, timeline) early so the visible explanation path exists before any module is deepened.

## How will you check that explanations are useful and accurate?

**Design answer:** “We check each explanation against the exact observations and decision branch that produced it. Controlled cases cover exploration, ties, fallback, forced switches, missing records, and requested versus actual execution. Those checks establish fidelity to the system's records. A separate user study would be needed to establish improved comprehension.”

For an optional pilot, compare raw logs with formatted explanations using counterbalanced order. Ask what was requested, why, and whether it happened; report answer accuracy, response time, participant count, and limitations. Without that study, say the system provides readable, traceable explanations, not that comprehension improved by a measured amount.

## What final integration defaults were selected?

- **Startup:** N/S green, elapsed green zero; observe and confirm before advancing. The time-zero check skips selection for minimum green.
- **Ties:** Absolute tolerance 1e-9 for comparing scores/Q-values, with no relative tolerance; current green wins when operating rules permit it.
- **Partial training coverage:** Frozen evaluation uses queue-based fallback if any permitted action lacks a completed training update; disclose that reason separately from an entirely unseen state.
- **Unexpected timing block:** Discard the new request, preserve earlier completed learning, and create no new learning interval or exploration-decay step. Keep observing and request a fresh decision at the next eligible scheduled check.
- **Episode scope:** Run to the configured horizon, even if traffic becomes empty. Natural early-terminal learning is outside this version.
- **Minimal architecture:** Use sensor, controller, actuator, adapter, independent safety monitor, runtime, persistence, explanation formatter (with CLI and static run report), and runner. Omit unused corridor/network supervisor scaffolding.

**Design closeout.** These routine defaults and the remaining experiment design were finalized under the user's authorization to complete the document. All modules are specified; implementation, tests, benchmarks, and any human-comprehension evaluation remain future work.


## Can this be built without the old XCPS repository?

**Design answer:** “Yes. The implementation specification defines a new Python package, direct SUMO/TraCI integration, generated network and vehicle demand, configuration, typed component interfaces, event/checkpoint formats, runtime ordering, and CLI commands. No old code, network file, checkpoint, or historical run is required.”

The main document's Standalone build contract is authoritative. Build the queue-based end-to-end slice first, then learning, persistence, and full reports. Using direct TraCI makes the new adapter the sole owner of one-second stepping and signal transitions; SUMO-RL is not a dependency. Queue normalization is explicitly defined from measured halting counts and lane length rather than inherited from another implementation.

Offline explanations restore the decision-time policy revision from an immutable run-start table and logged updates; using the final Q-table would misrepresent earlier decisions. The monitor has a narrowly defined startup exception and checks every low-level signal command. Native dependencies, generated networks, and the actual simulation still require smoke validation during implementation; the specification is not a claim that runnable code already exists.
