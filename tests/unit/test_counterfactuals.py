"""Module 7 / Section G: decision-time table replay, contrastive explanations, fidelity checks.

All without SUMO, on hand-built tables and synthetic run logs.
"""

import json

import pytest

from traffic_cps.config import load_config
from traffic_cps.contracts import Direction, Mode, QState
from traffic_cps.explain import (
    explain,
    fixed_contrast,
    load_trace,
    q_counterfactual,
    queue_contrast,
)
from traffic_cps.learning import Learner, QTable, add_sample, close_interval, open_interval
from tests.unit.factories import observation
from tests.unit.runs import RunBuilder

NS, WE = Direction.NS, Direction.WE
CONFIG = load_config("configs/default.yaml")
TOL = 1e-9


def table(cells, eb=1, green=NS):
    """cells: {(ns_bin, we_bin): (Q_NS, Q_WE, count_NS, count_WE)}"""
    t = QTable()
    for (ns, we), (qn, qw, cn, cw) in cells.items():
        s = QState(ns, we, green, eb)
        t._set(s, NS, qn, cn)
        t._set(s, WE, qw, cw)
    return t


# ----------------------------------------------------------------------
# Q-learning counterfactual search (Section G order and eligibility)
# ----------------------------------------------------------------------

KEEP, SWITCH = (-1.0, -2.0, 3, 3), (-3.0, -1.0, 3, 3)   # greedy NS / greedy WE, fully covered


def test_sectionG_counterfactual_nearest_bin_change_with_ns_then_we_order():
    t = table({(1, 1): KEEP, (0, 1): KEEP, (1, 0): KEEP, (1, 2): SWITCH, (2, 1): SWITCH})
    cf = q_counterfactual(t, QState(1, 1, NS, 1), NS, 10, TOL)
    # Distance-1 candidates in order (0,1), (1,0), (1,2), (2,1): (1,2) is the first that flips.
    assert cf["status"] == "found" and cf["state"] == QState(1, 2, NS, 1) and cf["distance"] == 1
    assert cf["action"] is WE and cf["changed"] == [("WE", 1, 2)] and cf["assumptions"] == []


def test_sectionG_counterfactual_skips_candidates_without_complete_coverage():
    t = table({(1, 1): KEEP, (1, 2): (-3.0, -1.0, 3, 0), (2, 2): SWITCH})
    cf = q_counterfactual(t, QState(1, 1, NS, 1), NS, 10, TOL)
    assert cf["state"] == QState(2, 2, NS, 1) and cf["distance"] == 2
    assert cf["changed"] == [("NS", 1, 2), ("WE", 1, 2)]


def test_sectionG_counterfactual_uses_current_green_tie_rule():
    tied = (-1.0, -1.0 + 5e-10, 2, 2)                      # within tolerance: NS kept, so no flip
    t = table({(1, 1): KEEP, (1, 2): tied, (1, 3): SWITCH})
    assert q_counterfactual(t, QState(1, 1, NS, 1), NS, 10, TOL)["state"] == QState(1, 3, NS, 1)


def test_sectionG_counterfactual_none_when_no_supported_candidate():
    t = table({(1, 1): KEEP, (0, 0): KEEP})
    assert q_counterfactual(t, QState(1, 1, NS, 1), NS, 10, TOL) == {"status": "none"}


def test_sectionG_counterfactual_at_service_limit_requires_empty_competing_queue():
    # Elapsed >= 60: candidates with a nonzero competing (WE) bin are timing-forced and excluded;
    # competing bin 0 is allowed only under an explicit zero-demand assumption.
    t = table({(1, 0): KEEP, (1, 1): SWITCH, (3, 0): SWITCH}, eb=3)
    cf = q_counterfactual(t, QState(1, 0, NS, 3), NS, 75, TOL)
    assert cf["state"] == QState(3, 0, NS, 3) and cf["changed"] == [("NS", 1, 3)]
    assert any("exactly zero" in a for a in cf["assumptions"])


def test_sectionG_counterfactual_result_is_verified_and_minimal():
    t = table({(a, b): (SWITCH if a + b >= 5 else KEEP) for a in range(5) for b in range(5)})
    cf = q_counterfactual(t, QState(1, 1, NS, 1), NS, 10, TOL)
    from traffic_cps.controllers.q_learning import greedy_choice
    assert greedy_choice(t.values(cf["state"]), NS, TOL)[0] is WE         # recomputation yields the claim
    assert cf["distance"] == 3                                             # (1,1) -> sum 5 needs 3 steps
    assert cf["state"] == QState(1, 4, NS, 1)                              # first in NS-then-WE order at d=3


# ----------------------------------------------------------------------
# Queue and fixed-time contrasts
# ----------------------------------------------------------------------

def test_sectionG_queue_contrast_states_threshold_with_tolerance_when_kept():
    text = queue_contrast({"NS": 0.5, "WE": 0.3}, "NS", "NS", "higher_queue_score", TOL)
    assert "W/E would have been requested only if its queue score exceeded 0.500000001" in text
    assert "it was 0.30" in text


def test_sectionG_queue_contrast_when_switched():
    text = queue_contrast({"NS": 0.24, "WE": 0.62}, "NS", "WE", "higher_queue_score", TOL)
    assert "N/S would have been kept if the W/E queue score had been at most 0.240000001" in text


def test_sectionG_queue_contrast_not_applicable_for_service_limit():
    assert "service limit" in queue_contrast({"NS": 1.0, "WE": 0.1}, "NS", "WE",
                                             "service_limit_competing_demand", TOL)


def test_K_fixed_contrast_states_switch_threshold():
    assert "would have requested W/E once N/S green reached 30s (it was 20s)" in fixed_contrast("NS", "NS", 20, 30)
    assert "would have kept N/S only while its green was below 30s (it was 33s)" in fixed_contrast("NS", "WE", 33, 30)


# ----------------------------------------------------------------------
# Decision-time replay, fidelity, later learning (synthetic logs)
# ----------------------------------------------------------------------

class Zero:
    def random(self):
        return 0.99   # never explore

    def choice(self, seq):
        raise AssertionError


def q_run(tmp_path, tamper=None):
    """Training log: policy_start, decision at 10 (greedy NS), update at 15, decision at 15 using revision 1."""
    b = RunBuilder(tmp_path)
    lr = Learner(CONFIG, rng=Zero())
    lr.schedule.epsilon = 0.0
    s = QState(0, 1, NS, 1)
    lr.table._set(s, NS, -1.0, 2)
    lr.table._set(s, WE, -2.0, 2)
    lr.table._set(QState(0, 2, NS, 1), NS, -3.0, 1)
    lr.table._set(QState(0, 2, NS, 1), WE, -1.0, 1)
    lr.table.revision = 0
    b.policy_start(lr)
    o10 = b.obs(time=10, ns_score=0.1, we_score=0.5, ns_count=3, we_count=15, elapsed=10)
    d10 = b.q_decide(o10, "dec-10", lr, Mode.TRAIN)
    w = open_interval("dec-10", d10.q_state, d10.requested, 10)
    for t in range(11, 16):
        w = add_sample(w, observation(time=t, ns_score=0.1, we_score=0.5))
    u = close_interval(lr.table, w, d10.q_state, (NS, WE), 15, alpha=0.1, d=0.95 ** 0.2)
    if tamper:
        u = tamper(u)
    b.learn(u)
    o15 = b.obs(time=15, ns_score=0.1, we_score=0.5, ns_count=3, we_count=15, elapsed=15)
    b.q_decide(o15, "dec-15", lr, Mode.TRAIN)
    return b.close(), d10, u


def test_sectionF_decision_time_table_is_replayed_from_start_plus_updates(tmp_path):
    run_dir, d10, u = q_run(tmp_path)
    trace = load_trace(run_dir)
    assert trace.policy.table_at(0).values(d10.q_state) == {NS: -1.0, WE: -2.0}
    assert trace.policy.table_at(1).values(d10.q_state)[NS] == u.new_q
    later = explain(run_dir, "dec-15")
    assert later.facts["policy_revision"] == 1 and later.facts["fidelity"] == "verified"


def test_module7_counterfactual_uses_decision_time_values_not_later_ones(tmp_path):
    run_dir, d10, u = q_run(tmp_path)
    x = explain(run_dir, "dec-10")
    assert x.facts["q_values"] == {"NS": -1.0, "WE": -2.0}                 # recorded before the update
    assert x.facts["counterfactual"]["state"] == [0, 2, "NS", 1]
    assert "W/E green would be preferred if the W/E queue category were bin 2" in x.contrast


def test_module7_greedy_wording_uses_estimated_discounted_cost_never_score(tmp_path):
    run_dir, _, _ = q_run(tmp_path)
    text = explain(run_dir, "dec-10").text
    assert ("kept N/S green because its estimated discounted future congestion cost was lower: "
            "1.00 versus 2.00") in text
    assert "Q-value" not in text and "learned score" not in text


def test_module7_later_learning_is_reported_separately_from_the_decision(tmp_path):
    run_dir, _, u = q_run(tmp_path)
    x = explain(run_dir, "dec-10")
    assert x.later is not None and "at 15s" in x.later and "after 5s of feedback" in x.later
    assert f"{-u.new_q:.2f}" in x.later and f"{-u.new_q:.2f}" not in x.text


def test_sectionF_tampered_update_breaks_replay_and_is_reported_not_hidden(tmp_path):
    from dataclasses import replace
    run_dir, _, _ = q_run(tmp_path, tamper=lambda u: replace(u, old_q=-7.0))
    x = explain(run_dir, "dec-15")
    assert x.facts["fidelity"] == "unavailable" and x.facts["counterfactual"] is None
    assert any("cannot be reconstructed" in lim for lim in x.limitations)


def test_module7_fidelity_check_flags_a_recorded_branch_that_the_values_do_not_support(tmp_path):
    run_dir, _, _ = q_run(tmp_path)
    path = run_dir / "events.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for e in lines:
        if e.get("event_id") == "dec-10":
            e["payload"]["requested"] = "WE"                               # inconsistent with recorded values
    path.write_text("".join(json.dumps(e) + "\n" for e in lines))
    x = explain(run_dir, "dec-10")
    assert x.facts["fidelity"] == "mismatch" and any("fidelity check failed" in lim for lim in x.limitations)


def test_module7_untrained_actions_are_labeled_untrained_not_zero_cost(tmp_path):
    b = RunBuilder(tmp_path)
    lr = Learner(CONFIG, rng=Zero())
    lr.schedule.epsilon = 0.0
    b.policy_start(lr)
    o = b.obs(time=10, ns_score=0.1, we_score=0.5, elapsed=10)
    b.q_decide(o, "dec-10", lr, Mode.TRAIN)
    x = explain(b.close(), "dec-10")
    assert "untrained" in x.text and "0.00" not in x.text
    assert "not applicable" in x.contrast.lower()                                  # incomplete coverage: no counterfactual


def test_module7_exploration_contrast_is_not_a_greedy_counterfactual(tmp_path):
    class Explore:
        def random(self):
            return 0.0

        def choice(self, seq):
            return NS

    b = RunBuilder(tmp_path)
    lr = Learner(CONFIG, rng=Explore())
    lr.table._set(QState(0, 1, NS, 1), NS, -1.0, 2)
    lr.table._set(QState(0, 1, NS, 1), WE, -2.0, 2)
    b.policy_start(lr)
    o = b.obs(time=10, ns_score=0.1, we_score=0.5, elapsed=10)
    b.q_decide(o, "dec-10", lr, Mode.TRAIN)
    x = explain(b.close(), "dec-10")
    assert "explored" in x.text and "matched" in x.text                   # random pick happened to equal greedy
    assert "not applicable" in x.contrast.lower() and x.facts["fidelity"] == "verified"
