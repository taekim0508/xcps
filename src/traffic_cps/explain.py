"""Explanation layer (Module 7, build contract Section G).

Reads a finished run's evidence (event log, manifest, policy_start.json) and
states, from linked records only, why a decision or event happened, what the
signal then did, and what learning followed. It never launches SUMO and never
selects actions.

For Q-learning decisions the decision-time table is rebuilt from
policy_start.json plus the ordered learning updates up to the decision's policy
revision, validating each old value. Every policy decision is re-checked
against its recorded branch (fidelity) using the same pure selection rules the
controllers use, and gets a contrast: a deterministic counterfactual for
covered greedy choices, the exact threshold for the queue rule and the fixed
schedule, and "not applicable" for exploration, fallback, holds, and safety.
Missing or inconsistent evidence becomes an explicit limitation, never a guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from traffic_cps.contracts import Direction, QState
from traffic_cps.events import EVENTS_FILE, POLICY_START, RUN_MANIFEST
from traffic_cps.learning import LearningError, QTable, state_from_key

NAMES = {"NS": "N/S", "WE": "W/E"}
OTHER = {"NS": "WE", "WE": "NS"}
_ENVELOPE = {"schema_version", "run_id", "event_id", "seq", "sim_time_s", "type", "payload"}
TIE_TOL = 1e-9          # recorded in the configuration as learning.tie_abs_tolerance
SCORE_CAP = 2.0
COST = "estimated discounted future congestion cost"


class ExplainError(RuntimeError):
    """The requested event cannot be explained (unknown ID or unsupported type)."""


class PolicyHistory:
    """Decision-time Q-tables: policy_start.json plus ordered learning updates, old values validated."""

    def __init__(self, start: dict, updates: list[dict]):
        self.start = start
        self.updates = updates
        self._table: QTable | None = None
        self._next = 0  # index into updates of the next one to apply
        self.problem: str | None = None

    def table_at(self, revision: int) -> QTable:
        if self._table is None or self._table.revision > revision:
            self._table, self._next = QTable.from_json(self.start["table"]), 0
        if revision < self._table.revision:
            raise ExplainError(f"revision {revision} precedes the run's starting revision {self._table.revision}")
        while self._table.revision < revision:
            if self._next >= len(self.updates):
                raise ExplainError(f"learning updates up to revision {revision} are missing from the log")
            p = self.updates[self._next]["payload"]
            self._next += 1
            if p["policy_revision"] != self._table.revision + 1:
                raise ExplainError(f"update {self.updates[self._next - 1]['event_id']} has revision "
                                   f"{p['policy_revision']}, expected {self._table.revision + 1}")
            state, action = state_from_key(p["state"]), Direction(p["action"])
            if self._table.values(state)[action] != p["old_q"] \
                    or self._table.update_counts(state)[action] != p["update_count_before"]:
                raise ExplainError(f"update {self.updates[self._next - 1]['event_id']} does not match the "
                                   f"replayed table (old value or count differs)")
            self._table._set(state, action, p["new_q"], p["update_count_after"])
            self._table.ensure(state_from_key(p["next_state"]))
            self._table.revision = p["policy_revision"]
        return self._table.copy()


@dataclass
class RunTrace:
    run_dir: Path
    manifest: dict
    events: list[dict]
    by_id: dict[str, dict]
    problems: list[str] = field(default_factory=list)
    policy: PolicyHistory | None = None   # None for baselines (policy_start.json is null)

    @property
    def complete(self) -> bool:
        return not self.problems

    def later_learning(self, decision_id: str) -> dict | None:
        return next((e for e in self.events if e["type"] in ("learning_update", "feedback_discarded")
                     and e["payload"]["decision_id"] == decision_id), None)


@dataclass(frozen=True)
class Explanation:
    event_id: str
    event_type: str
    time: int
    text: str
    facts: dict
    limitations: list[str]
    trace_complete: bool
    contrast: str | None = None   # "why this rather than that", or why no contrast applies
    later: str | None = None      # learning that followed this decision, kept separate


def load_trace(run_dir: Path | str) -> RunTrace:
    run_dir = Path(run_dir)
    try:
        manifest = json.loads((run_dir / RUN_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExplainError(f"cannot read {run_dir / RUN_MANIFEST}: {exc}") from exc
    try:
        lines = (run_dir / EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ExplainError(f"cannot read {run_dir / EVENTS_FILE}: {exc}") from exc

    events, by_id, problems = [], {}, []
    last_seq = 0
    for number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except ValueError:
            problems.append(f"line {number} is not valid JSON")
            continue
        if not isinstance(event, dict) or set(event) != _ENVELOPE:
            problems.append(f"line {number} lacks the event envelope")
            continue
        if event["seq"] <= last_seq:
            problems.append(f"line {number} has non-increasing seq {event['seq']}")
        last_seq = max(last_seq, event["seq"])
        events.append(event)
        by_id[event["event_id"]] = event
    if not events or events[-1]["type"] != "run_end":
        problems.append("log has no run_end record")

    policy = None
    start_path = run_dir / POLICY_START
    if start_path.is_file():
        try:
            start = json.loads(start_path.read_text(encoding="utf-8"))
        except ValueError:
            start = None
            problems.append(f"{POLICY_START} is not valid JSON")
        if start is not None:
            policy = PolicyHistory(start, [e for e in events if e["type"] == "learning_update"])
    return RunTrace(run_dir, manifest, events, by_id, problems, policy)


# ----------------------------------------------------------------------
# Formatting helpers (display rounding only; facts stay unrounded)
# ----------------------------------------------------------------------

def name(direction: str) -> str:
    return NAMES[direction]


def pair(a: float, b: float) -> tuple[str, str]:
    """Format two values with enough decimals that unequal values never look equal."""
    for digits in range(2, 10):
        left, right = f"{a:.{digits}f}", f"{b:.{digits}f}"
        if left != right or a == b:
            return left, right
    return repr(a), repr(b)


def _situation(t: int, counts: dict, scores: dict) -> str:
    ns, we = pair(scores["NS"], scores["WE"])
    return (f"At {t}s, N/S had {counts['NS']} waiting vehicles (queue score {ns}) "
            f"and W/E had {counts['WE']} (queue score {we}).")


def _execution(trace: RunTrace, origin_id: str, requested: str, current: str | None) -> tuple[str, dict, list]:
    """What actually happened after a request, from submission and signal-change records only."""
    submission = next((e for e in trace.events if e["type"] == "submission"
                       and e["payload"]["origin"]["id"] == origin_id), None)
    changes = [e for e in trace.events if e["type"] == "signal_change" and e["payload"]["origin"]["id"] == origin_id]
    yellow = next((e for e in changes if e["payload"]["origin"]["kind"] in ("controller_decision", "operating_action")), None)
    green = next((e for e in changes if e["payload"]["origin"]["kind"] == "transition_completion"), None)
    facts = {
        "requested": requested,
        "submission_event_id": submission["event_id"] if submission else None,
        "status": submission["payload"]["status"] if submission else None,
        "yellow_at": yellow["sim_time_s"] if yellow else None,
        "green_at": green["sim_time_s"] if green else None,
        "signal_event_ids": [e["event_id"] for e in changes],
    }
    limitations: list[str] = []
    if submission is None:
        limitations.append("Submission record is missing; execution cannot be explained.")
        return "", facts, limitations

    status = submission["payload"]["status"]
    if status == "accepted" and requested == current:
        return f"No signal change was needed; {name(current)} stayed green.", facts, limitations
    if status == "accepted":
        if yellow is None:
            limitations.append("The yellow onset for this request is not in the log.")
            return f"The request to switch to {name(requested)} was submitted.", facts, limitations
        text = f"{name(OTHER[requested])} turned yellow at {yellow['sim_time_s']}s"
        if green is not None:
            return f"{text}, and {name(requested)} became green at {green['sim_time_s']}s.", facts, limitations
        limitations.append(f"{name(requested)} green was not observed in the log (the run ended or the trace "
                           f"is incomplete).")
        return f"{text}.", facts, limitations
    if status == "blocked":
        reason = submission["payload"]["block_reason"]
        return f"The request was blocked ({reason}); the signal did not change.", facts, limitations
    limitations.append(f"Submission {status}: execution is unconfirmed.")
    return f"Submission {status}: {submission['payload']['error']}.", facts, limitations


# ----------------------------------------------------------------------
# Contrasts: why this rather than that (Section G)
# ----------------------------------------------------------------------

def _bin(i: int) -> str:
    edges = (0.0, 0.4, 0.8, 1.2, 1.6, 2.0)
    close = "]" if i == 4 else ")"
    return f"bin {i} [{edges[i]}, {edges[i + 1]}{close}"


def q_counterfactual(table: QTable, state: QState, requested: Direction, elapsed_s: float, tol: float) -> dict:
    """Nearest queue-bin pair (Manhattan distance, then NS bin, then WE bin) whose greedy action differs.

    Holds the green and exact elapsed time fixed and uses only fully covered candidates. At or
    beyond the service limit a nonzero competing bin is timing-forced (excluded) and competing bin 0
    needs an explicit exactly-zero-demand assumption. Minimal means bin distance only.
    """
    from traffic_cps.controllers.q_learning import greedy_choice

    green = state.green
    competing_is_ns = green is Direction.WE
    candidates = []
    for ns in range(5):
        for we in range(5):
            if (ns, we) == (state.ns_bin, state.we_bin):
                continue
            cand = QState(ns, we, green, state.elapsed_bin)
            counts = table.update_counts(cand)
            if not table.contains(cand) or min(counts.values()) == 0:
                continue
            competing_bin = ns if competing_is_ns else we
            if state.elapsed_bin == 3 and competing_bin > 0:
                continue
            candidates.append((abs(ns - state.ns_bin) + abs(we - state.we_bin), ns, we, cand))
    for distance, ns, we, cand in sorted(candidates, key=lambda c: c[:3]):
        action = greedy_choice(table.values(cand), green, tol)[0]
        if action is not requested:
            changed = [(d, a, b) for d, a, b in (("NS", state.ns_bin, ns), ("WE", state.we_bin, we)) if a != b]
            assumptions = []
            if state.elapsed_bin == 3:
                assumptions.append(f"the {NAMES[green.other.value]} queue is exactly zero vehicles (bin 0 also "
                                   f"covers small positive scores, which would force a switch at this elapsed time)")
            return {"status": "found", "state": cand, "distance": distance, "action": action,
                    "changed": changed, "assumptions": assumptions, "values": table.values(cand),
                    "counts": table.update_counts(cand)}
    return {"status": "none"}


def queue_contrast(scores: dict, green: str, requested: str, reason: str, tol: float) -> str:
    """Exact queue-rule threshold, including the absolute tie tolerance."""
    if reason == "service_limit_competing_demand":
        return ("Not a queue comparison: the green-service limit with waiting vehicles decided this, so no "
                "queue threshold applies.")
    other = OTHER[green]
    own, competing = scores[green], scores[other]
    limit = own + tol
    if requested == green:
        return (f"{NAMES[other]} would have been requested only if its queue score exceeded {limit!r} "
                f"(the {NAMES[green]} score plus the 1e-9 tie tolerance); it was {competing:.2f}. "
                f"Timing constraints still apply.")
    return (f"{NAMES[green]} would have been kept if the {NAMES[other]} queue score had been at most {limit!r} "
            f"(the {NAMES[green]} score plus the 1e-9 tie tolerance); it was {competing:.2f}.")


def fixed_contrast(green: str, requested: str, elapsed_s: int, target_s: int) -> str:
    other = OTHER[green]
    if requested == green:
        return (f"The schedule would have requested {NAMES[other]} once {NAMES[green]} green reached "
                f"{target_s}s (it was {elapsed_s}s).")
    return f"The schedule would have kept {NAMES[green]} only while its green was below {target_s}s (it was {elapsed_s}s)."


# ----------------------------------------------------------------------
# Global policy view (Module 7, Section G)
# ----------------------------------------------------------------------

def policy_view(table: QTable, label: str, source: dict | None = None) -> dict:
    """Cell-by-cell view of a named table: for each green and elapsed bin, a 5 x 5 grid (N/S bin rows,
    W/E bin columns) with greedy action, cost gap, update counts, and the Section G labels."""
    from traffic_cps.controllers.q_learning import greedy_choice

    grids = []
    for green in (Direction.NS, Direction.WE):
        for eb in range(4):
            rows = []
            for ns in range(5):
                row = []
                for we in range(5):
                    competing_bin = we if green is Direction.NS else ns
                    c: dict[str, Any] = {"ns_bin": ns, "we_bin": we}
                    if eb == 0:
                        c["status"] = "unavailable"          # minimum green: never a free choice
                    elif eb == 3 and competing_bin > 0:
                        c["status"] = "constrained"          # service limit with waiting vehicles forces a switch
                    else:
                        state = QState(ns, we, green, eb)
                        counts = table.update_counts(state)
                        c["conditional"] = eb == 3           # eligible only with an exactly empty competing queue
                        if not table.contains(state):
                            c["status"] = "fallback_unseen"
                        elif min(counts.values()) == 0:
                            c["status"] = "fallback_incomplete"
                        else:
                            values = table.values(state)
                            greedy = greedy_choice(values, green, TIE_TOL)[0]
                            c.update(status="covered", greedy=greedy.value,
                                     costs={a.value: -v for a, v in values.items()},
                                     cost_gap=abs(values[Direction.NS] - values[Direction.WE]),
                                     counts={a.value: n for a, n in counts.items()})
                            if ns == we:
                                c["queue_rule"] = "ambiguous"
                            else:
                                preferred = "NS" if ns > we else "WE"
                                c["queue_rule"] = "agrees" if preferred == greedy.value else "disagrees"
                    row.append(c)
                rows.append(row)
            grids.append({"green": green.value, "elapsed_bin": eb, "cells": rows})
    return {"label": label, "source": source or {}, "revision": table.revision, "grids": grids}


def run_policy_views(trace: RunTrace) -> list[dict]:
    """Named views for a run: evaluation shows its frozen table; training shows episode start and end.
    Baselines have none."""
    if trace.policy is None:
        return []
    start = QTable.from_json(trace.policy.start["table"])
    provenance = trace.manifest.get("provenance") or {}
    if trace.manifest.get("mode") == "evaluate":
        ident = provenance.get("checkpoint_id") or "in-memory table"
        return [policy_view(start, f"frozen table ({ident})", provenance)]
    views = [policy_view(start, f"episode start (revision {start.revision})", provenance)]
    updates = trace.policy.updates
    if updates:
        end = trace.policy.table_at(updates[-1]["payload"]["policy_revision"])
        views.append(policy_view(end, f"episode end (revision {end.revision})", provenance))
    return views


# ----------------------------------------------------------------------
# Fidelity: re-check the recorded branch against the recorded evidence
# ----------------------------------------------------------------------

def _queue_recheck(p: dict) -> str | None:
    from traffic_cps.controllers.queue import queue_choice

    green = Direction(p["current_green"])
    requested, reason, tie, cap = queue_choice({Direction(k): v for k, v in p["scores"].items()},
                                               {Direction(k): v for k, v in p["counts"].items()}, green,
                                               p["elapsed_green_s"], p["green_service_limit_s"], TIE_TOL, SCORE_CAP)
    if (requested.value, reason.value, tie, cap) != (p["requested"], p["reason"], p["tie"], p["both_at_cap"]):
        return (f"the queue rule on the recorded scores gives {requested.value}/{reason.value}, but the record says "
                f"{p['requested']}/{p['reason']}")
    return None


def _fidelity(p: dict, table: QTable | None) -> str | None:
    """None if the recorded branch is reproduced by its rule on the recorded (and replayed) values."""
    from traffic_cps.controllers.q_learning import greedy_choice

    source = p["source"]
    if source == "queue_rule":
        return _queue_recheck(p)
    if source == "fixed_schedule":
        switch = p["elapsed_green_s"] >= p["fixed_green_target_s"]
        expected = ("fixed_target_reached", OTHER[p["current_green"]]) if switch else \
            ("fixed_target_not_reached", p["current_green"])
        return None if (p["reason"], p["requested"]) == expected else \
            f"the schedule gives {expected}, but the record says {(p['reason'], p['requested'])}"
    if source not in ("q_greedy", "q_exploration", "queue_fallback"):
        return None
    state = state_from_key(p["q_state"])
    values = {Direction(k): v for k, v in p["q_values"].items()}
    if table is not None:
        if table.values(state) != values or {a.value: c for a, c in table.update_counts(state).items()} \
                != p["q_update_counts"]:
            return "the recorded values or counts differ from the replayed decision-time table"
    if source == "q_exploration":
        return None if p["exploration_draw"] < p["epsilon"] else "exploration recorded although draw >= epsilon"
    if source == "queue_fallback":
        if table is not None:
            unseen = not table.contains(state)
            expected = "unseen_state" if unseen else (
                "incomplete_action_coverage" if min(table.update_counts(state).values()) == 0 else None)
            if expected != p["fallback_reason"]:
                return f"fallback reason {p['fallback_reason']} is not supported by the table ({expected})"
        return _queue_recheck(p)
    greedy, reason, _ = greedy_choice(values, Direction(p["current_green"]), TIE_TOL)
    if (greedy.value, reason.value) != (p["requested"], p["reason"]):
        return f"the greedy rule on the recorded values gives {greedy.value}/{reason.value}, not {p['requested']}/{p['reason']}"
    return None


# ----------------------------------------------------------------------
# Per-type explanations
# ----------------------------------------------------------------------

def _queue_reason(p: dict, verb: str) -> str:
    """The queue rule's recorded branch (also used when Q-learning falls back to it)."""
    req, cur, scores, counts = p["requested"], p["current_green"], p["scores"], p["counts"]
    if p["reason"] == "higher_queue_score":
        mine, theirs = pair(scores[req], scores[OTHER[req]])
        return f"The queue rule {verb} because its queue score was higher: {mine} versus {theirs}."
    if p["reason"] == "equal_queue_scores":
        if p["both_at_cap"]:
            return (f"N/S and W/E are both at estimated lane capacity; queue scores cannot distinguish "
                    f"them, so the tie rule kept {name(cur)} green.")
        value = pair(scores[cur], scores[OTHER[cur]])[0]
        return f"The queue scores were equal ({value}), so the tie rule kept {name(cur)} green."
    if p["reason"] == "service_limit_competing_demand":
        return (f"The queue rule requested {name(req)} green because {name(cur)} had been green for "
                f"{p['elapsed_green_s']}s, reaching the {p['green_service_limit_s']}s service limit, while "
                f"{counts[req]} vehicles were waiting on {name(req)}.")
    raise ExplainError(f"unsupported queue-rule reason {p['reason']!r}")


def _cost(p: dict, d: str) -> str:
    """-Q as the estimated discounted future congestion cost; zero-update entries are untrained, not zero cost."""
    n = p["q_update_counts"][d]
    if n == 0:
        return f"{name(d)} untrained (0 updates)"
    return f"{name(d)} {-p['q_values'][d]:.2f} ({n} update{'s' if n != 1 else ''})"


def _q_choice(p: dict, verb: str) -> str:
    req, cur = p["requested"], p["current_green"]
    q, counts = p["q_values"], p["q_update_counts"]
    state = (f"Learned state: N/S queue {_bin(p['q_state'][0])}, W/E queue {_bin(p['q_state'][1])}, elapsed-green "
             f"bin {p['q_state'][3]}; policy revision {p['policy_revision']}. {COST[0].upper() + COST[1:]} (lower "
             f"preferred): {_cost(p, 'NS')}, {_cost(p, 'WE')}.")
    if p["reason"] == "exploration":
        from traffic_cps.controllers.q_learning import greedy_choice

        greedy = greedy_choice({Direction(k): v for k, v in q.items()}, Direction(cur), TIE_TOL)[0].value
        relation = "matched" if greedy == req else "differed from"
        return (f"{state} During training the controller explored: its random draw {p['exploration_draw']:.3f} was "
                f"below the exploration rate {p['epsilon']:.3f}, so it {verb} at random to gather experience. "
                f"Learned values did not determine this choice (it {relation} the lower-cost choice, "
                f"{name(greedy)}).")
    if p["reason"] == "higher_q_value":
        chosen, alt = pair(-q[req], -q[OTHER[req]])
        return f"{state} The learned policy {verb} because its {COST} was lower: {chosen} versus {alt}."
    if p["reason"] == "equal_q_values":
        if counts["NS"] == 0 and counts["WE"] == 0:
            return (f"{state} Both actions are untrained in this state, so their initial values are equal and the "
                    f"tie rule kept {name(cur)} green.")
        return f"{state} The estimated costs were equal, so the tie rule kept {name(cur)} green."
    raise ExplainError(f"unsupported Q-learning reason {p['reason']!r}")


def _q_contrast(trace: RunTrace, p: dict, table: QTable | None) -> tuple[str, dict | None]:
    source = p["source"]
    if source == "q_exploration":
        return "Not applicable: this was random exploration; no table comparison determined it.", None
    if source == "queue_fallback":
        return ("Not applicable for the learned policy (fallback). Queue-rule contrast: "
                + queue_contrast(p["scores"], p["current_green"], p["requested"], p["reason"], TIE_TOL)), None
    if min(p["q_update_counts"].values()) == 0:
        return ("Not applicable: not every action has a completed training update in this state, so the "
                "decision-time values are not a fully trained comparison."), None
    if table is None:
        return "Unavailable: the decision-time table could not be reconstructed.", None
    state = state_from_key(p["q_state"])
    cf = q_counterfactual(table, state, Direction(p["requested"]), p["elapsed_green_s"], TIE_TOL)
    if cf["status"] == "none":
        return ("No supported counterfactual: no fully trained queue-bin combination with the same green and "
                "elapsed time prefers the other action under the decision-time table."), {"status": "none"}
    changes = " and ".join(f"the {NAMES[d]} queue category were {_bin(b)} instead of {_bin(a)}"
                           for d, a, b in cf["changed"])
    text = (f"Under the decision-time table, {name(cf['action'].value)} green would be preferred if {changes} "
            f"(bin distance {cf['distance']}; minimal in bins, not in vehicles or a realizable traffic path).")
    if cf["assumptions"]:
        text += " Assumes " + "; ".join(cf["assumptions"]) + "."
    facts = {"status": "found", "state": cf["state"].key(), "distance": cf["distance"],
             "action": cf["action"].value, "changed": cf["changed"], "assumptions": cf["assumptions"]}
    return text, facts


def _later(trace: RunTrace, decision_id: str) -> str | None:
    e = trace.later_learning(decision_id)
    if e is None:
        return None
    p = e["payload"]
    if e["type"] == "feedback_discarded":
        return (f"Later (not used for this decision): at {e['sim_time_s']}s this choice's unfinished feedback was "
                f"discarded ({p['reason']}); no learning update was made from it.")
    return (f"Later (not used for this decision): at {e['sim_time_s']}s, after {p['duration_s']}s of feedback, "
            f"the {COST} of choosing {name(p['action'])} in this state was updated from {-p['old_q']:.2f} to "
            f"{-p['new_q']:.2f} (update {p['update_count_after']}, policy revision {p['policy_revision']}).")


def _decision(trace: RunTrace, e: dict) -> tuple[str, dict, list, str | None, str | None]:
    p, t = e["payload"], e["sim_time_s"]
    req, cur = p["requested"], p["current_green"]
    scores, counts = p["scores"], p["counts"]
    parts = [_situation(t, counts, scores)]
    verb = f"kept {name(cur)} green" if req == cur else f"requested {name(req)} green"
    source = p["source"]
    limitations: list[str] = []
    contrast, cf_facts, table = None, None, None
    is_q = source in ("q_greedy", "q_exploration", "queue_fallback")

    if is_q:
        if trace.policy is None:
            limitations.append("policy_start.json is missing; the decision-time table cannot be reconstructed.")
        else:
            try:
                table = trace.policy.table_at(p["policy_revision"])
            except (ExplainError, LearningError, KeyError, ValueError) as exc:
                limitations.append(f"The decision-time table cannot be reconstructed: {exc}")

    if source == "test_script":
        parts.append(f"A test script {verb}.")
    elif source == "queue_rule":
        parts.append(_queue_reason(p, verb))
        contrast = queue_contrast(scores, cur, req, p["reason"], TIE_TOL)
    elif source == "fixed_schedule" and p["reason"] == "fixed_target_reached":
        parts.append(f"The fixed-time schedule requested {name(req)} green because {name(cur)} had been green "
                     f"for {p['elapsed_green_s']}s, reaching its {p['fixed_green_target_s']}s fixed green target. "
                     f"Queue scores did not affect this choice.")
        contrast = fixed_contrast(cur, req, p["elapsed_green_s"], p["fixed_green_target_s"])
    elif source == "fixed_schedule" and p["reason"] == "fixed_target_not_reached":
        parts.append(f"The fixed-time schedule kept {name(cur)} green because it had been green for "
                     f"{p['elapsed_green_s']}s, below its {p['fixed_green_target_s']}s fixed green target. "
                     f"Queue scores did not affect this choice.")
        contrast = fixed_contrast(cur, req, p["elapsed_green_s"], p["fixed_green_target_s"])
    elif source in ("q_greedy", "q_exploration"):
        parts.append(_q_choice(p, verb))
        contrast, cf_facts = _q_contrast(trace, p, table)
    elif source == "queue_fallback":
        why = ("no training experience was recorded for this state" if p["fallback_reason"] == "unseen_state"
               else "not all available actions have completed training experience in this state")
        parts.append(f"The frozen learned policy did not decide: {why} ({p['fallback_reason']}), so the "
                     f"queue rule was used instead.")
        parts.append(_queue_reason(p, verb))
        contrast, cf_facts = _q_contrast(trace, p, table)
    else:
        raise ExplainError(f"unsupported decision source/reason {source!r}/{p['reason']!r}")

    if source == "test_script":
        fidelity = "not_applicable"
    elif is_q and table is None:
        fidelity = "unavailable"
    else:
        problem = _fidelity(p, table)
        fidelity = "verified" if problem is None else "mismatch"
        if problem:
            limitations.append(f"fidelity check failed: {problem}")

    exec_text, exec_facts, exec_limits = _execution(trace, e["event_id"], req, cur)
    if exec_text:
        parts.append(exec_text)
    limitations += exec_limits
    if is_q:
        limitations.append("Values are learned estimates, not guaranteed costs or confidence measures.")
    facts = {
        "observation_id": p["observation_id"], "source": source, "reason": p["reason"],
        "requested": req, "current_green": cur, "counts": counts, "scores": scores,
        "tie": p["tie"], "both_at_cap": p["both_at_cap"], "elapsed_green_s": p["elapsed_green_s"],
        "fixed_green_target_s": p.get("fixed_green_target_s"),
        "q_state": p.get("q_state"), "q_values": p.get("q_values"), "q_update_counts": p.get("q_update_counts"),
        "policy_revision": p.get("policy_revision"), "epsilon": p.get("epsilon"),
        "fallback_reason": p.get("fallback_reason"), "execution": exec_facts,
        "fidelity": fidelity, "counterfactual": cf_facts,
    }
    later = _later(trace, e["event_id"]) if is_q else None
    return " ".join(parts), facts, limitations, contrast, later


def _learning_update(trace: RunTrace, e: dict):
    p, t = e["payload"], e["sim_time_s"]
    text = (f"At {t}s, the training choice {p['decision_id']} ({name(p['action'])} from state {p['state']}, made at "
            f"{p['start_time_s']}s) was updated after {p['duration_s']}s of feedback: raw reward sum "
            f"{p['raw_reward_sum']:.3f}, discounted {p['discounted_reward']:.3f}, next-state estimate "
            f"{p['bootstrap_max']:.3f} times {p['bootstrap_multiplier']:.4f}, target {p['target']:.3f}. "
            f"Its value changed from {p['old_q']:.3f} to {p['new_q']:.3f} (update {p['update_count_after']} for "
            f"this entry; policy revision {p['policy_revision']}).")
    return text, dict(p), [], None, None


def _feedback_discarded(trace: RunTrace, e: dict):
    p, t = e["payload"], e["sim_time_s"]
    text = (f"At {t}s, the unfinished feedback for {p['decision_id']} (started {p['start_time_s']}s, "
            f"{len(p['rewards'])} samples) was discarded ({p['reason']}); no learning update was made from it.")
    return text, dict(p), [], None, None


def _operating_action(trace: RunTrace, e: dict):
    p, t = e["payload"], e["sim_time_s"]
    green, req = p["green"], p["requested"]
    text = (f"At {t}s, {name(green)} had been green for {p['elapsed_green_s']}s, reaching the "
            f"{p['green_service_limit_s']}s green-service limit, while {p['competing_measured_count']} vehicles "
            f"were measured waiting on {name(req)}. The operating rule, not the controller, required a switch "
            f"to {name(req)}.")
    exec_text, exec_facts, limitations = _execution(trace, e["event_id"], req, green)
    facts = {"reason": p["reason"], "counts": p["counts"], "scores": p["scores"],
             "elapsed_green_s": p["elapsed_green_s"], "execution": exec_facts}
    return (" ".join(x for x in (text, exec_text) if x), facts, limitations,
            "Not applicable: an operating rule, not a controller choice.", None)


def _constraint_hold(trace: RunTrace, e: dict):
    p, t = e["payload"], e["sim_time_s"]
    if p["reason"] == "minimum_green_not_reached":
        why = (f"minimum green not reached ({name(p['current_green'])} green for {p['elapsed_green_s']}s; "
               f"minimum {p['minimum_green_s']}s).")
    else:
        why = (f"a transition to {name(p['transition_target'])} was in progress "
               f"(yellow since {p['transition_start_s']}s).")
    return (f"At {t}s (scheduled control point), no controller choice was made: {why}", dict(p), [],
            "Not applicable: a timing constraint, not a controller choice.", None)


def _safety(trace: RunTrace, e: dict):
    p, t = e["payload"], e["sim_time_s"]
    contrast = "Not applicable: a safety-monitor event, not a controller choice."
    if e["type"] == "safety_override":
        return (f"At {t}s, the safety monitor blocked a signal change ({p['invariant']}): {p['reason']}.",
                dict(p), [], contrast, None)
    return (f"At {t}s, the safety monitor observed a {p['invariant']} violation: {p['detail']}. "
            f"The run stopped.", dict(p), [], contrast, None)


_HANDLERS = {
    "decision": _decision,
    "operating_action": _operating_action,
    "constraint_hold": _constraint_hold,
    "safety_override": _safety,
    "safety_violation": _safety,
    "learning_update": _learning_update,
    "feedback_discarded": _feedback_discarded,
}


def explain_event(trace: RunTrace, event_id: str) -> Explanation:
    e = trace.by_id.get(event_id)
    if e is None:
        raise ExplainError(f"no event {event_id!r} in {trace.run_dir}")
    handler = _HANDLERS.get(e["type"])
    if handler is None:
        raise ExplainError(f"event {event_id} has type {e['type']!r}, which has no explanation")
    text, facts, limitations, contrast, later = handler(trace, e)
    if not trace.complete:
        limitations = [*limitations, f"Event log is incomplete ({'; '.join(trace.problems)}); "
                                     f"linked evidence may be missing."]
    facts = {"event_id": event_id, "type": e["type"], "time": e["sim_time_s"], **facts}
    return Explanation(event_id, e["type"], e["sim_time_s"], text, facts, limitations, trace.complete,
                       contrast, later)


def explain(run_dir: Path | str, event_id: str) -> Explanation:
    return explain_event(load_trace(run_dir), event_id)


def render_text(x: Explanation) -> str:
    lines = [f"[{x.event_type} {x.event_id} at {x.time}s]", x.text]
    if x.contrast:
        lines.append(f"Alternative: {x.contrast}")
    if x.later:
        lines.append(x.later)
    if x.limitations:
        lines.append("Limitations:")
        lines += [f"  - {lim}" for lim in x.limitations]
    return "\n".join(lines)
