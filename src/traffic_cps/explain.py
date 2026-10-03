"""Explanation layer, minimal (Module 7, build contract Section G).

Reads a finished run's event log and states, from linked evidence only, why a
decision or operating-rule event happened and what the signal then did.
Selection, execution, and limitations are kept separate. An outcome is stated
only when its confirming event exists. This module never launches SUMO.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from traffic_cps.events import EVENTS_FILE, RUN_MANIFEST

NAMES = {"NS": "N/S", "WE": "W/E"}
OTHER = {"NS": "WE", "WE": "NS"}
_ENVELOPE = {"schema_version", "run_id", "event_id", "seq", "sim_time_s", "type", "payload"}


class ExplainError(RuntimeError):
    """The requested event cannot be explained (unknown ID or unsupported type)."""


@dataclass
class RunTrace:
    run_dir: Path
    manifest: dict
    events: list[dict]
    by_id: dict[str, dict]
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class Explanation:
    event_id: str
    event_type: str
    time: int
    text: str
    facts: dict
    limitations: list[str]
    trace_complete: bool


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
    return RunTrace(run_dir, manifest, events, by_id, problems)


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
# Per-type explanations
# ----------------------------------------------------------------------

def _decision(trace: RunTrace, e: dict) -> tuple[str, dict, list]:
    p, t = e["payload"], e["sim_time_s"]
    req, cur = p["requested"], p["current_green"]
    scores, counts = p["scores"], p["counts"]
    parts = [_situation(t, counts, scores)]
    verb = f"kept {name(cur)} green" if req == cur else f"requested {name(req)} green"

    if p["source"] == "test_script":
        parts.append(f"A test script {verb}.")
    elif p["reason"] == "higher_queue_score":
        mine, theirs = pair(scores[req], scores[OTHER[req]])
        parts.append(f"The queue rule {verb} because its queue score was higher: {mine} versus {theirs}.")
    elif p["reason"] == "equal_queue_scores":
        if p["both_at_cap"]:
            parts.append(f"N/S and W/E are both at estimated lane capacity; queue scores cannot distinguish "
                         f"them, so the tie rule kept {name(cur)} green.")
        else:
            value = pair(scores[cur], scores[OTHER[cur]])[0]
            parts.append(f"The queue scores were equal ({value}), so the tie rule kept {name(cur)} green.")
    elif p["reason"] == "service_limit_competing_demand":
        parts.append(f"The queue rule requested {name(req)} green because {name(cur)} had been green for "
                     f"{p['elapsed_green_s']}s, reaching the {p['green_service_limit_s']}s service limit, while "
                     f"{counts[req]} vehicles were waiting on {name(req)}.")
    else:
        raise ExplainError(f"unsupported decision reason {p['reason']!r}")

    exec_text, exec_facts, limitations = _execution(trace, e["event_id"], req, cur)
    if exec_text:
        parts.append(exec_text)
    facts = {
        "observation_id": p["observation_id"], "source": p["source"], "reason": p["reason"],
        "requested": req, "current_green": cur, "counts": counts, "scores": scores,
        "tie": p["tie"], "both_at_cap": p["both_at_cap"], "elapsed_green_s": p["elapsed_green_s"],
        "execution": exec_facts,
    }
    return " ".join(parts), facts, limitations


def _operating_action(trace: RunTrace, e: dict) -> tuple[str, dict, list]:
    p, t = e["payload"], e["sim_time_s"]
    green, req = p["green"], p["requested"]
    text = (f"At {t}s, {name(green)} had been green for {p['elapsed_green_s']}s, reaching the "
            f"{p['green_service_limit_s']}s green-service limit, while {p['competing_measured_count']} vehicles "
            f"were measured waiting on {name(req)}. The operating rule, not the controller, required a switch "
            f"to {name(req)}.")
    exec_text, exec_facts, limitations = _execution(trace, e["event_id"], req, green)
    facts = {"reason": p["reason"], "counts": p["counts"], "scores": p["scores"],
             "elapsed_green_s": p["elapsed_green_s"], "execution": exec_facts}
    return " ".join(x for x in (text, exec_text) if x), facts, limitations


def _constraint_hold(trace: RunTrace, e: dict) -> tuple[str, dict, list]:
    p, t = e["payload"], e["sim_time_s"]
    if p["reason"] == "minimum_green_not_reached":
        why = (f"minimum green not reached ({name(p['current_green'])} green for {p['elapsed_green_s']}s; "
               f"minimum {p['minimum_green_s']}s).")
    else:
        why = (f"a transition to {name(p['transition_target'])} was in progress "
               f"(yellow since {p['transition_start_s']}s).")
    return f"At {t}s (scheduled control point), no controller choice was made: {why}", dict(p), []


def _safety(trace: RunTrace, e: dict) -> tuple[str, dict, list]:
    p, t = e["payload"], e["sim_time_s"]
    if e["type"] == "safety_override":
        return (f"At {t}s, the safety monitor blocked a signal change ({p['invariant']}): {p['reason']}.",
                dict(p), [])
    return (f"At {t}s, the safety monitor observed a {p['invariant']} violation: {p['detail']}. "
            f"The run stopped.", dict(p), [])


_HANDLERS = {
    "decision": _decision,
    "operating_action": _operating_action,
    "constraint_hold": _constraint_hold,
    "safety_override": _safety,
    "safety_violation": _safety,
}


def explain_event(trace: RunTrace, event_id: str) -> Explanation:
    e = trace.by_id.get(event_id)
    if e is None:
        raise ExplainError(f"no event {event_id!r} in {trace.run_dir}")
    handler = _HANDLERS.get(e["type"])
    if handler is None:
        raise ExplainError(f"event {event_id} has type {e['type']!r}, which has no explanation")
    text, facts, limitations = handler(trace, e)
    if not trace.complete:
        limitations = [*limitations, f"Event log is incomplete ({'; '.join(trace.problems)}); "
                                     f"linked evidence may be missing."]
    facts = {"event_id": event_id, "type": e["type"], "time": e["sim_time_s"], **facts}
    return Explanation(event_id, e["type"], e["sim_time_s"], text, facts, limitations, trace.complete)


def explain(run_dir: Path | str, event_id: str) -> Explanation:
    return explain_event(load_trace(run_dir), event_id)


def render_text(x: Explanation) -> str:
    lines = [f"[{x.event_type} {x.event_id} at {x.time}s]", x.text]
    if x.limitations:
        lines.append("Limitations:")
        lines += [f"  - {lim}" for lim in x.limitations]
    return "\n".join(lines)
