"""Module 7 global policy view / Section G grid labels, cell by cell against a known table."""

from traffic_cps.contracts import Direction, QState
from traffic_cps.explain import policy_view
from traffic_cps.learning import QTable

NS, WE = Direction.NS, Direction.WE


def grid(view, green, eb):
    return next(g for g in view["grids"] if g["green"] == green and g["elapsed_bin"] == eb)


def cell(view, green, eb, ns, we):
    return grid(view, green, eb)["cells"][ns][we]


def known_table():
    t = QTable()
    t._set(QState(2, 0, NS, 1), NS, -1.0, 4)        # covered, keeps NS (cost 1 vs 3)
    t._set(QState(2, 0, NS, 1), WE, -3.0, 2)
    t._set(QState(1, 3, NS, 1), NS, -5.0, 1)        # covered, switches to WE
    t._set(QState(1, 3, NS, 1), WE, -2.0, 1)
    t._set(QState(1, 1, NS, 1), NS, -2.0, 3)        # covered, equal bins: ambiguous vs queue rule
    t._set(QState(1, 1, NS, 1), WE, -4.0, 3)
    t._set(QState(3, 1, NS, 1), NS, -1.0, 2)        # covered, but disagrees: queue rule would keep NS (3 > 1)
    t._set(QState(3, 1, NS, 1), WE, -0.5, 2)
    t._set(QState(0, 2, NS, 2), NS, -1.0, 5)        # incomplete coverage
    t._set(QState(0, 2, NS, 2), WE, 0.0, 0)
    t.ensure(QState(0, 1, NS, 2))                   # seen but never updated: incomplete
    t._set(QState(2, 0, NS, 3), NS, -1.0, 1)        # bin 3, competing bin 0: conditionally eligible
    t._set(QState(2, 0, NS, 3), WE, -2.0, 1)
    return t


def test_module7_grid_shape_two_greens_four_elapsed_bins_5x5():
    v = policy_view(known_table(), "test")
    assert len(v["grids"]) == 8
    assert all(len(g["cells"]) == 5 and all(len(r) == 5 for r in g["cells"]) for g in v["grids"])


def test_module7_covered_cell_shows_greedy_gap_and_counts():
    c = cell(policy_view(known_table(), "t"), "NS", 1, 2, 0)
    assert c["status"] == "covered" and c["greedy"] == "NS" and c["cost_gap"] == 2.0
    assert c["counts"] == {"NS": 4, "WE": 2} and c["costs"] == {"NS": 1.0, "WE": 3.0}
    assert cell(policy_view(known_table(), "t"), "NS", 1, 1, 3)["greedy"] == "WE"


def test_module7_fallback_cells_marked_unseen_or_incomplete():
    v = policy_view(known_table(), "t")
    assert cell(v, "NS", 2, 0, 2)["status"] == "fallback_incomplete"
    assert cell(v, "NS", 2, 0, 1)["status"] == "fallback_incomplete"      # zero entry, never updated
    assert cell(v, "NS", 2, 4, 4)["status"] == "fallback_unseen"


def test_sectionG_minimum_green_bin_unavailable_and_bin3_constrained_or_conditional():
    v = policy_view(known_table(), "t")
    assert {c["status"] for row in grid(v, "NS", 0)["cells"] for c in row} == {"unavailable"}
    assert cell(v, "NS", 3, 2, 1)["status"] == "constrained"              # competing W/E bin 1 at >= 60 s
    c = cell(v, "NS", 3, 2, 0)
    assert c["status"] == "covered" and c["conditional"] is True           # only with exactly empty W/E queue
    assert cell(v, "WE", 3, 1, 0)["status"] == "constrained"              # for W/E green, N/S competes
    assert cell(v, "WE", 3, 0, 3)["status"] != "constrained"


def test_sectionG_queue_rule_comparison_only_where_bins_are_unequal():
    v = policy_view(known_table(), "t")
    assert cell(v, "NS", 1, 2, 0)["queue_rule"] == "agrees"
    assert cell(v, "NS", 1, 1, 3)["queue_rule"] == "agrees"
    assert cell(v, "NS", 1, 3, 1)["queue_rule"] == "disagrees"
    assert cell(v, "NS", 1, 1, 1)["queue_rule"] == "ambiguous"            # equal bins: raw scores may differ


def test_module7_policy_view_records_its_source_identity():
    v = policy_view(known_table(), "frozen checkpoint t-g00028", {"checkpoint_id": "t-g00028"})
    assert v["label"] == "frozen checkpoint t-g00028" and v["source"]["checkpoint_id"] == "t-g00028"
    assert v["revision"] == 0
