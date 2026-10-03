"""Build small synthetic run directories with the runtime's own payload builders."""

import json
from pathlib import Path

from traffic_cps import events as ev
from traffic_cps.contracts import (
    BlockReason,
    Direction,
    Origin,
    OriginKind,
    SafetyVerdict,
    SignalColor,
    SignalEvent,
    SubmissionResult,
    SubmissionStatus,
)
from traffic_cps.controllers.queue import QueueController
from tests.unit.factories import context, observation

NS, WE = Direction.NS, Direction.WE
OK = SafetyVerdict(True, None, "allowed")
COLORS = {
    "GGrr": {NS: SignalColor.GREEN, WE: SignalColor.RED},
    "yyrr": {NS: SignalColor.YELLOW, WE: SignalColor.RED},
    "rrGG": {NS: SignalColor.RED, WE: SignalColor.GREEN},
}


def signal(event_id, t, old, new, origin, target):
    return SignalEvent(event_id, t, old, new, COLORS[old], COLORS[new], origin, target, OK)


class RunBuilder:
    def __init__(self, root: Path, run_id="run-test"):
        self.dir = ev.create_run_dir(root, run_id)
        (self.dir / ev.RUN_MANIFEST).write_text(json.dumps({"run_id": run_id, "controller": "queue",
                                                            "horizon_s": 60}))
        self.w = ev.EventWriter(self.dir / ev.EVENTS_FILE, run_id)
        init = Origin(OriginKind.INITIALIZATION, "init")
        self.w.append("signal_change", 0, ev.signal_change_payload(signal("sig-1", 0, "GGrr", "GGrr", init, None)))

    def obs(self, **kw):
        o = observation(**kw)
        self.w.append("observation", o.time, ev.observation_payload(o), event_id=o.observation_id)
        return o

    def decide(self, o, decision_id):
        d = QueueController().select(context(o), decision_id)
        self.w.append("decision", o.time, ev.decision_payload(d), event_id=d.decision_id)
        return d

    def submit(self, d, status=SubmissionStatus.ACCEPTED, event=None, block=None, mask="rrGG"):
        origin = Origin(OriginKind.CONTROLLER_DECISION, d.decision_id)
        result = SubmissionResult(origin, d.time, d.requested, status, block_reason=block, signal_event=event)
        self.w.append("submission", d.time, ev.submission_payload(result, mask))
        if event is not None:
            self.w.append("signal_change", event.time, ev.signal_change_payload(event))

    def complete(self, t, origin_id, target=WE):
        origin = Origin(OriginKind.TRANSITION_COMPLETION, origin_id)
        self.w.append("signal_change", t, ev.signal_change_payload(signal(f"sig-{t}", t, "yyrr", "rrGG", origin, target)))

    def raw(self, line: str):
        self.w.close()
        with (self.dir / ev.EVENTS_FILE).open("a") as fh:
            fh.write(line + "\n")

    def close(self):
        self.w.append("run_end", 60, {"completion": "completed", "failure": None, "planned_horizon_s": 60,
                                      "final_valid_time_s": 60, "counters": {}})
        self.w.close()
        return self.dir


def switch_run(root, with_green=True):
    """N/S green; at 10s the queue rule requests W/E; yellow at 10, (optionally) green at 12."""
    b = RunBuilder(root)
    o = b.obs(time=10, ns_score=0.24, we_score=0.62, ns_count=8, we_count=20, elapsed=10)
    d = b.decide(o, "dec-10")
    origin = Origin(OriginKind.CONTROLLER_DECISION, "dec-10")
    b.submit(d, event=signal("sig-2", 10, "GGrr", "yyrr", origin, WE))
    if with_green:
        b.complete(12, "dec-10")
    return b.close()
