"""Module 3 (actuator): validation, decision linkage, submission is not execution (no SUMO)."""

from dataclasses import replace

import pytest

from traffic_cps.actuator import ActuatorError, TrafficActuator
from traffic_cps.contracts import (
    Direction,
    OriginKind,
    SignalPhase,
    SubmissionResult,
    SubmissionStatus,
)
from traffic_cps.controllers.queue import QueueController
from tests.unit.factories import context, observation

NS, WE = Direction.NS, Direction.WE


class FakeMap:
    def mask(self, phase):
        return {"NS_GREEN": "GGrr", "WE_GREEN": "rrGG", "NS_YELLOW": "yyrr", "WE_YELLOW": "rryy"}[phase.value]


class FakeAdapter:
    signal_map = FakeMap()

    def __init__(self):
        self.calls = []

    def request(self, target, origin, observation=None):
        self.calls.append((target, origin, observation))
        return SubmissionResult(origin, observation.time, target, SubmissionStatus.ACCEPTED)


def decision(we_score=0.9):
    obs = observation(ns_score=0.1, we_score=we_score, ns_count=2, we_count=25)
    return QueueController().select(context(obs), "dec-7"), obs


@pytest.mark.parametrize("we_score,target,mask", [(0.9, WE, "rrGG"), (0.0, NS, "GGrr")])
def test_module3_valid_requests_map_to_masks_and_keep_decision_reference(we_score, target, mask):
    adapter = FakeAdapter()
    d, obs = decision(we_score)
    submission = TrafficActuator(adapter).submit(d, obs)
    assert submission.result.target is target
    assert submission.target_mask == mask
    origin = adapter.calls[0][1]
    assert origin.kind is OriginKind.CONTROLLER_DECISION and origin.origin_id == "dec-7"


def test_module3_submission_success_is_not_confirmation():
    adapter = FakeAdapter()
    d, obs = decision()
    submission = TrafficActuator(adapter).submit(d, obs)
    assert submission.result.status is SubmissionStatus.ACCEPTED
    assert submission.result.signal_event is None  # nothing observed by the actuator itself
    assert not hasattr(submission, "confirmed")


@pytest.mark.parametrize("bad", ["WE", None, 1, "EW"])
def test_module3_invalid_request_errors_without_submitting(bad):
    adapter = FakeAdapter()
    d, obs = decision()
    with pytest.raises(ActuatorError):
        TrafficActuator(adapter).submit(replace(d, requested=bad), obs)
    assert adapter.calls == []


def test_module3_decision_must_match_its_observation():
    adapter = FakeAdapter()
    d, obs = decision()
    with pytest.raises(ActuatorError):
        TrafficActuator(adapter).submit(d, observation(time=15))
    with pytest.raises(ActuatorError):
        TrafficActuator(adapter).submit(None, obs)
    assert adapter.calls == []


def test_module3_submission_error_cause_is_retained():
    class Failing(FakeAdapter):
        def request(self, target, origin, observation=None):
            return SubmissionResult(origin, observation.time, target, SubmissionStatus.FAILED,
                                    error="signal write failed: connection reset")

    d, obs = decision()
    submission = TrafficActuator(Failing()).submit(d, obs)
    assert submission.result.status is SubmissionStatus.FAILED
    assert "connection reset" in submission.result.error
