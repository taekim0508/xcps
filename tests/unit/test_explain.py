"""Module 7 (minimal explainer) and Section G wording rules, from linked evidence only."""

import pytest

from traffic_cps.contracts import BlockReason, SubmissionStatus
from traffic_cps.explain import ExplainError, explain
from tests.unit.runs import RunBuilder, switch_run


def test_module7_switch_explanation_uses_recorded_branch_and_values(tmp_path):
    e = explain(switch_run(tmp_path), "dec-10")
    assert "At 10s, N/S had 8 waiting vehicles (queue score 0.24) and W/E had 20 (queue score 0.62)." in e.text
    assert "requested W/E green because its queue score was higher: 0.62 versus 0.24" in e.text
    assert e.facts["source"] == "queue_rule" and e.facts["reason"] == "higher_queue_score"
    assert e.facts["scores"] == {"NS": 0.24, "WE": 0.62}  # unrounded evidence
    assert e.facts["counts"] == {"NS": 8, "WE": 20}


def test_module7_execution_separate_from_selection_and_confirmed_by_events(tmp_path):
    e = explain(switch_run(tmp_path), "dec-10")
    assert "N/S turned yellow at 10s, and W/E became green at 12s." in e.text
    assert e.facts["execution"]["requested"] == "WE"
    assert e.facts["execution"]["yellow_at"] == 10 and e.facts["execution"]["green_at"] == 12
    assert e.limitations == []


def test_module7_green_not_claimed_without_confirming_event(tmp_path):
    e = explain(switch_run(tmp_path, with_green=False), "dec-10")
    assert "became green" not in e.text
    assert e.facts["execution"]["green_at"] is None
    assert any("not observed" in lim for lim in e.limitations)


def test_module7_retain_is_explained_as_a_decision(tmp_path):
    b = RunBuilder(tmp_path)
    o = b.obs(time=20, ns_score=1.1, we_score=0.4, ns_count=33, we_count=12, elapsed=20)
    b.submit(b.decide(o, "dec-20"))
    e = explain(b.close(), "dec-20")
    assert "kept N/S green because its queue score was higher: 1.10 versus 0.40" in e.text
    assert "No signal change was needed" in e.text


def test_module7_saturated_tie_says_so(tmp_path):
    b = RunBuilder(tmp_path)
    o = b.obs(time=30, ns_score=2.0, we_score=2.0, ns_count=70, we_count=66, elapsed=30)
    b.submit(b.decide(o, "dec-30"))
    e = explain(b.close(), "dec-30")
    assert ("N/S and W/E are both at estimated lane capacity; queue scores cannot distinguish them, "
            "so the tie rule kept N/S green.") in e.text


def test_module7_ordinary_tie_wording(tmp_path):
    b = RunBuilder(tmp_path)
    o = b.obs(time=30, ns_score=0.0, we_score=0.0, elapsed=30)
    b.submit(b.decide(o, "dec-30"))
    assert "queue scores were equal (0.00), so the tie rule kept N/S green" in explain(b.close(), "dec-30").text


def test_module7_close_scores_are_not_rounded_into_a_false_tie(tmp_path):
    b = RunBuilder(tmp_path)
    o = b.obs(time=30, ns_score=0.501, we_score=0.5012, ns_count=10, we_count=10, elapsed=30)
    b.submit(b.decide(o, "dec-30"))
    assert "0.5012 versus 0.5010" in explain(b.close(), "dec-30").text


def test_module7_blocked_request_is_reported_not_executed(tmp_path):
    b = RunBuilder(tmp_path)
    o = b.obs(time=10, ns_score=0.1, we_score=0.9, we_count=25, elapsed=10)
    b.submit(b.decide(o, "dec-10"), status=SubmissionStatus.BLOCKED, block=BlockReason.SAFETY_VETO)
    e = explain(b.close(), "dec-10")
    assert "blocked (safety_veto)" in e.text and "turned yellow" not in e.text


def test_module7_unknown_event_is_an_error(tmp_path):
    with pytest.raises(ExplainError):
        explain(switch_run(tmp_path), "dec-999")


def test_module7_malformed_line_marks_trace_incomplete(tmp_path):
    b = RunBuilder(tmp_path)
    o = b.obs(time=10, ns_score=0.24, we_score=0.62, ns_count=8, we_count=20, elapsed=10)
    b.submit(b.decide(o, "dec-10"))
    b.raw("{not json")
    e = explain(b.dir, "dec-10")
    assert e.trace_complete is False
    assert any("incomplete" in lim for lim in e.limitations)


def test_sectionJ_fixed_switch_explained_as_fixed_schedule(tmp_path):
    from traffic_cps.controllers.fixed import FixedTimeController

    b = RunBuilder(tmp_path)
    o = b.obs(time=35, ns_score=0.9, we_score=0.1, ns_count=25, we_count=3, elapsed=33)
    b.submit(b.decide(o, "dec-35", FixedTimeController(30)))
    e = explain(b.close(), "dec-35")
    assert ("The fixed-time schedule requested W/E green because N/S had been green for 33s, "
            "reaching its 30s fixed green target.") in e.text
    assert "Queue scores did not affect this choice." in e.text
    assert "queue score was higher" not in e.text and "operating rule" not in e.text
    assert e.facts["source"] == "fixed_schedule" and e.facts["reason"] == "fixed_target_reached"
    assert e.facts["fixed_green_target_s"] == 30


def test_sectionJ_fixed_retain_explained_as_fixed_schedule(tmp_path):
    from traffic_cps.controllers.fixed import FixedTimeController

    b = RunBuilder(tmp_path)
    o = b.obs(time=20, ns_score=0.1, we_score=1.5, ns_count=2, we_count=45, elapsed=20)
    b.submit(b.decide(o, "dec-20", FixedTimeController(30)))
    e = explain(b.close(), "dec-20")
    assert ("The fixed-time schedule kept N/S green because it had been green for 20s, "
            "below its 30s fixed green target.") in e.text
    assert "No signal change was needed" in e.text


def _demand_fault_run(tmp_path, kind, true_c, measured_c, elapsed):
    b = RunBuilder(tmp_path)
    b.w.append(kind, 64, {"kind": kind, "observation_id": "obs-64", "green": "NS", "competing": "WE",
                          "elapsed_green_s": elapsed, "green_service_limit_s": 60,
                          "measured_competing_count": measured_c, "true_competing_count": true_c,
                          "fault_label": "high", "operating_event_id": "op-64" if kind == "phantom_demand" else None},
               event_id=f"{kind}-64")
    return b.close()


def test_module8_phantom_demand_explained_with_seen_versus_true(tmp_path):
    x = explain(_demand_fault_run(tmp_path, "phantom_demand", 0, 1, 60), "phantom_demand-64")
    assert ("forced a switch to W/E because the sensor reported 1 waiting on W/E; the simulator's true count "
            "was 0") in x.text
    assert "not a safety violation" in x.text and "not applicable" in x.contrast.lower()


def test_module8_masked_demand_explained_with_seen_versus_true(tmp_path):
    x = explain(_demand_fault_run(tmp_path, "masked_demand", 3, 0, 64), "masked_demand-64")
    assert "3 vehicles were truly waiting on W/E, but the sensor reported 0, so no switch was forced" in x.text
