"""Module 8 tuning choice and aggregate statistics, on hand-computed numbers (no SUMO)."""

import statistics

import pytest

from traffic_cps.experiments import ExperimentError, aggregate_metric, choose_fixed_target, development_only


def test_module8_tuning_picks_lowest_mean_over_development_seeds():
    chosen, table = choose_fixed_target({20: [5.0, 7.0, 6.0], 25: [4.0, 6.0, 5.0], 30: [6.0, 6.0, 6.0]})
    assert chosen == 25
    assert table[25]["mean"] == 5.0 and table[20]["mean"] == 6.0


def test_module8_tuning_ties_go_to_the_shorter_target():
    chosen, _ = choose_fixed_target({35: [4.0, 4.0], 30: [3.0, 5.0], 40: [4.5, 4.5]})
    assert chosen == 30


def test_module8_tuning_excludes_candidates_with_incomplete_runs():
    chosen, table = choose_fixed_target({15: [1.0, None, 1.0], 20: [3.0, 3.0, 3.0]})
    assert chosen == 20 and table[15]["eligible"] is False and table[15]["incomplete_seeds"] == 1


def test_module8_tuning_with_no_eligible_candidate_fails_explicitly():
    with pytest.raises(ExperimentError, match="no candidate"):
        choose_fixed_target({20: [None], 25: [None]})


def test_sectionJ_tuning_never_reads_final_seeds():
    assert development_only([100, 101, 102], final=[200, 201]) == [100, 101, 102]
    with pytest.raises(ExperimentError, match="final"):
        development_only([100, 200], final=[200, 201])


def test_module8_aggregate_mean_sd_and_paired_differences_against_each_baseline():
    per_seed = {
        "fixed": {200: 10.0, 201: 12.0, 202: 8.0},
        "queue": {200: 6.0, 201: 7.0, 202: 5.0},
        "q_learning": {200: 7.0, 201: 6.0, 202: 6.0},
    }
    a = aggregate_metric(per_seed, baselines=("fixed", "queue"))
    assert a["policies"]["queue"]["mean"] == 6.0
    assert a["policies"]["queue"]["sd"] == pytest.approx(statistics.stdev([6.0, 7.0, 5.0]))
    d = a["paired"]["q_learning_vs_queue"]
    assert d["differences"] == {200: 1.0, 201: -1.0, 202: 1.0} and d["mean_difference"] == pytest.approx(1 / 3)
    assert d["relative_change_of_means"] == pytest.approx((19 / 3 - 6.0) / 6.0)
    assert a["paired"]["queue_vs_fixed"]["mean_difference"] == pytest.approx(-4.0)


def test_module8_incomplete_runs_are_excluded_from_aggregates_but_counted():
    a = aggregate_metric({"fixed": {200: 10.0, 201: None}, "queue": {200: 6.0, 201: 5.0}}, baselines=("fixed",))
    assert a["policies"]["fixed"]["n"] == 1 and a["policies"]["fixed"]["incomplete_seeds"] == [201]
    assert a["policies"]["fixed"]["sd"] is None                         # one complete seed: no sample SD
    assert a["paired"]["queue_vs_fixed"]["seeds"] == [200]              # pairs only where both completed


def test_module8_relative_change_only_when_baseline_nonzero():
    a = aggregate_metric({"fixed": {200: 0.0, 201: 2.0}, "queue": {200: 1.0, 201: 1.0}}, baselines=("fixed",))
    d = a["paired"]["queue_vs_fixed"]
    assert d["relative_change_by_seed"] == {200: None, 201: -0.5}
