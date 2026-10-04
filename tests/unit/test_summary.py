"""Module 7 episode summary and Module 8 metric definitions on a synthetic run (no SUMO)."""

import csv

from traffic_cps.contracts import SubmissionStatus
from traffic_cps.report import summarize, write_summary
from traffic_cps.explain import load_trace
from tests.unit.runs import RunBuilder, switch_run


def small_run(tmp_path):
    b = RunBuilder(tmp_path)
    b.obs(time=0, ns_count=9, we_count=9)                  # t = 0 is context, not a metric sample
    b.obs(time=1, ns_count=2, we_count=4)
    b.obs(time=2, ns_count=0, we_count=0)                  # zero samples count
    o = b.obs(time=3, ns_score=0.1, we_score=0.6, ns_count=3, we_count=7, elapsed=10)
    b.submit(b.decide(o, "dec-3"))
    o = b.obs(time=4, ns_score=0.9, we_score=0.2, ns_count=10, we_count=1, elapsed=10)
    b.submit(b.decide(o, "dec-4"), status=SubmissionStatus.BLOCKED)
    return b


def test_module8_mean_peak_and_exposure_use_samples_1_to_H(tmp_path):
    b = small_run(tmp_path)
    b.w.append("run_end", 4, {"completion": "completed", "failure": None, "planned_horizon_s": 4,
                              "final_valid_time_s": 4, "counters": {}, "checkpoint_status": None})
    b.w.close()
    s = summarize(load_trace(b.dir))
    m = s["metrics"]
    # totals at t=1..4: 6, 0, 10, 11 -> exposure 27, mean 27/4, peak 11; t=0 (18) excluded
    assert m["queue_exposure_vehicle_seconds"] == 27 and m["mean_queued_vehicles"] == 27 / 4
    assert m["peak_queued_vehicles"] == 11 and m["samples"] == 4 and m["partial"] is False
    assert m["mean_queued_by_direction"] == {"NS": 15 / 4, "WE": 12 / 4}


def test_K_vehicle_accounting_reported_as_not_recorded(tmp_path):
    s = summarize(load_trace(switch_run(tmp_path)))
    assert s["metrics"]["completed_vehicles"] == "not_recorded"
    assert s["metrics"]["remaining_demand"] == "not_recorded"


def test_module7_summary_counts_sources_with_denominators_and_notable_decisions(tmp_path):
    s = summarize(load_trace(switch_run(tmp_path)))
    b = s["behavior"]
    assert b["decisions"] == 1 and b["by_source"] == {"queue_rule": 1}
    assert b["switch_requests_accepted"] == 1
    assert [n["event_id"] for n in s["notable"]["switches"]["items"]] == ["dec-10"]
    assert s["notable"]["switches"]["total"] == 1


def test_module8_incomplete_run_metrics_are_labeled_partial(tmp_path):
    b = small_run(tmp_path)
    b.w.append("run_end", 4, {"completion": "incomplete", "failure": "x", "planned_horizon_s": 60,
                              "final_valid_time_s": 4, "counters": {}, "checkpoint_status": None})
    b.w.close()
    s = summarize(load_trace(b.dir))
    assert s["metrics"]["partial"] is True and s["run"]["completion"] == "incomplete"
    assert s["metrics"]["samples"] == 4                    # mean over observed seconds only, labeled partial


def test_K_metrics_csv_is_one_header_and_one_row(tmp_path):
    run_dir = switch_run(tmp_path)
    summary_path, csv_path = write_summary(run_dir)
    rows = list(csv.reader(csv_path.open()))
    assert len(rows) == 2 and rows[0][0] == "run_id" and "mean_queued_vehicles" in rows[0]
    assert summary_path.name == "summary.json"
