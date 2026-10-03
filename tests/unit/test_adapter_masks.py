"""Section B/D: signal masks derived from controlled links, never assumed (no SUMO)."""

import pytest

from traffic_cps.adapter import AdapterError, SignalMap
from traffic_cps.contracts import Direction, SignalColor, SignalPhase

LANES = {Direction.NS: ("N_J_0", "N_J_1"), Direction.WE: ("W_J_0", "W_J_1")}


def link(inc, out):
    return [(inc, out, f":J0_{inc}")]


def test_contract_B_masks_for_ns_ns_we_we_order():
    links = [link("N_J_0", "J_S_0"), link("N_J_1", "J_S_1"), link("W_J_0", "J_E_0"), link("W_J_1", "J_E_1")]
    sm = SignalMap.from_controlled_links(links, LANES)
    assert sm.mask(SignalPhase.NS_GREEN) == "GGrr"
    assert sm.mask(SignalPhase.NS_YELLOW) == "yyrr"
    assert sm.mask(SignalPhase.WE_GREEN) == "rrGG"
    assert sm.mask(SignalPhase.WE_YELLOW) == "rryy"


def test_contract_B_masks_follow_actual_index_order():
    links = [link("W_J_1", "J_E_1"), link("N_J_0", "J_S_0"), link("W_J_0", "J_E_0"), link("N_J_1", "J_S_1")]
    sm = SignalMap.from_controlled_links(links, LANES)
    assert sm.index_directions == (Direction.WE, Direction.NS, Direction.WE, Direction.NS)
    assert sm.mask(SignalPhase.NS_GREEN) == "rGrG"
    assert sm.mask(SignalPhase.WE_YELLOW) == "yryr"


def test_contract_B_movements_reported_from_links():
    links = [link("N_J_0", "J_S_0"), link("N_J_1", "J_S_1"), link("W_J_0", "J_E_0"), link("W_J_1", "J_E_1")]
    sm = SignalMap.from_controlled_links(links, LANES)
    assert sm.movements == (("N_J_0", "J_S_0"), ("N_J_1", "J_S_1"), ("W_J_0", "J_E_0"), ("W_J_1", "J_E_1"))


def test_contract_B_decode_colors():
    sm = SignalMap.from_controlled_links(
        [link("N_J_0", "J_S_0"), link("N_J_1", "J_S_1"), link("W_J_0", "J_E_0"), link("W_J_1", "J_E_1")], LANES)
    assert sm.colors("yyrr") == {Direction.NS: SignalColor.YELLOW, Direction.WE: SignalColor.RED}
    for bad in ("Gyrr", "GGr", "GGrx"):
        with pytest.raises(AdapterError):
            sm.colors(bad)


@pytest.mark.parametrize(
    "links",
    [
        [link("N_J_0", "J_S_0"), link("N_J_1", "J_S_1"), link("W_J_0", "J_E_0")],               # lane missing
        [link("N_J_0", "J_S_0"), link("N_J_1", "J_S_1"), link("W_J_0", "J_E_0"), []],           # empty index
        [link("N_J_0", "J_S_0"), link("N_J_1", "J_S_1"), link("W_J_0", "J_E_0"), link("X_0", "Y_0")],  # unknown lane
        [link("N_J_0", "J_S_0"), [("N_J_1", "J_S_1", "v"), ("W_J_1", "J_E_1", "v")],
         link("W_J_0", "J_E_0"), link("W_J_1", "J_E_1")],                                        # mixed directions
    ],
)
def test_contract_B_invalid_mapping_stops_initialization(links):
    with pytest.raises(AdapterError):
        SignalMap.from_controlled_links(links, LANES)
