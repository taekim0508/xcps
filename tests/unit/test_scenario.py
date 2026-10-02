"""Build contract Section B: network source XML and demand generation (no SUMO)."""

import json

import pytest

from traffic_cps.scenario import (
    DemandProfile,
    ScenarioError,
    direction_probabilities,
    generate_demand,
    network_source_files,
    profile_for_index,
    write_demand,
)


def test_contract_B_network_source_xml_is_deterministic():
    assert network_source_files() == network_source_files()


def test_contract_B_network_source_lists_only_four_straight_connections():
    files = network_source_files()
    connections = files["connections.con.xml"]
    assert connections.count("<connection ") == 4
    for frm, to, lane in [("N_J", "J_S", 0), ("N_J", "J_S", 1), ("W_J", "J_E", 0), ("W_J", "J_E", 1)]:
        assert f'from="{frm}" to="{to}" fromLane="{lane}" toLane="{lane}"' in connections


def test_contract_B_network_nodes_and_edges():
    files = network_source_files()
    nodes = files["nodes.nod.xml"]
    assert 'id="J0" x="0.00" y="0.00" type="traffic_light"' in nodes
    for node, x, y in [("N", "0.00", "250.00"), ("S", "0.00", "-250.00"), ("W", "-250.00", "0.00"), ("E", "250.00", "0.00")]:
        assert f'id="{node}" x="{x}" y="{y}" type="priority"' in nodes
    edges = files["edges.edg.xml"]
    assert edges.count("<edge ") == 4
    for edge, frm, to in [("N_J", "N", "J0"), ("J_S", "J0", "S"), ("W_J", "W", "J0"), ("J_E", "J0", "E")]:
        assert f'id="{edge}" from="{frm}" to="{to}" priority="1" numLanes="2" speed="13.90"' in edges


def test_contract_B_peak_probability_boundaries():
    # H=6: H/3=2, 2H/3=4.
    peak = DemandProfile.PEAK
    assert [direction_probabilities(peak, t, 6) for t in range(6)] == [
        (0.15, 0.15), (0.15, 0.15), (0.45, 0.45), (0.45, 0.45), (0.20, 0.20), (0.20, 0.20)
    ]


def test_contract_B_fixed_profile_probabilities():
    assert direction_probabilities(DemandProfile.BALANCED, 0, 60) == (0.30, 0.30)
    assert direction_probabilities(DemandProfile.ASYMMETRIC, 59, 60) == (0.20, 0.50)


def test_contract_B_demand_hand_computed_balanced_seed0():
    # Random(0) draws (NS, WE) per second: (.8444,.7580) (.4206,.2589) (.5113,.4049)
    # (.7838,.3033) (.4766,.5834). Only WE at t=1 is below 0.30.
    vehicles = generate_demand(seed=0, profile=DemandProfile.BALANCED, horizon_s=5)
    assert [(v.vehicle_id, v.depart_s, v.route, v.depart_lane) for v in vehicles] == [("veh0", 1, "WE", 0)]


def test_contract_B_demand_hand_computed_asymmetric_alternates_lanes():
    # Same draws; WE < 0.50 at t=1,2,3; NS never < 0.20.
    vehicles = generate_demand(seed=0, profile=DemandProfile.ASYMMETRIC, horizon_s=5)
    assert [(v.vehicle_id, v.depart_s, v.route, v.depart_lane) for v in vehicles] == [
        ("veh0", 1, "WE", 0),
        ("veh1", 2, "WE", 1),
        ("veh2", 3, "WE", 0),
    ]


def test_contract_B_demand_orders_ns_before_we_and_alternates_per_direction():
    vehicles = generate_demand(seed=7, profile=DemandProfile.PEAK, horizon_s=300)
    keys = [(v.depart_s, 0 if v.route == "NS" else 1) for v in vehicles]
    assert keys == sorted(keys)
    assert [v.vehicle_id for v in vehicles] == [f"veh{i}" for i in range(len(vehicles))]
    for direction in ("NS", "WE"):
        lanes = [v.depart_lane for v in vehicles if v.route == direction]
        assert lanes == [i % 2 for i in range(len(lanes))]


def test_contract_J_demand_xml_stable_for_fixed_seed(tmp_path):
    a = write_demand(tmp_path / "a", seed=3, profile=DemandProfile.BALANCED, horizon_s=120)
    b = write_demand(tmp_path / "b", seed=3, profile=DemandProfile.BALANCED, horizon_s=120)
    c = write_demand(tmp_path / "c", seed=4, profile=DemandProfile.BALANCED, horizon_s=120)
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() != c.read_bytes()


def test_contract_B_routes_xml_contains_vtype_and_routes(tmp_path):
    path = write_demand(tmp_path / "d", seed=0, profile=DemandProfile.ASYMMETRIC, horizon_s=5)
    text = path.read_text()
    for attr in [
        'id="car"', 'vClass="passenger"', 'length="5.00"', 'minGap="2.50"', 'maxSpeed="13.90"',
        'accel="2.60"', 'decel="4.50"', 'tau="1.00"', 'sigma="0.50"', 'carFollowModel="Krauss"',
        'speedFactor="1.00"', 'speedDev="0.00"',
    ]:
        assert attr in text
    assert '<route id="NS" edges="N_J J_S"/>' in text
    assert '<route id="WE" edges="W_J J_E"/>' in text
    assert ('<vehicle id="veh1" type="car" route="WE" depart="2" departLane="1" '
            'departPos="base" departSpeed="0"/>') in text


def test_contract_B_demand_refuses_existing_nonempty_directory(tmp_path):
    out = tmp_path / "taken"
    out.mkdir()
    (out / "x").write_text("")
    with pytest.raises(ScenarioError):
        write_demand(out, seed=0, profile=DemandProfile.BALANCED, horizon_s=5)


@pytest.mark.parametrize("horizon", [0, -5, 7])
def test_contract_B_demand_rejects_bad_horizon(horizon):
    with pytest.raises(ScenarioError):
        generate_demand(seed=0, profile=DemandProfile.BALANCED, horizon_s=horizon)


def test_contract_B_profile_cycles_by_seed_list_position():
    assert [profile_for_index(i) for i in range(5)] == [
        DemandProfile.BALANCED, DemandProfile.ASYMMETRIC, DemandProfile.PEAK,
        DemandProfile.BALANCED, DemandProfile.ASYMMETRIC,
    ]
    with pytest.raises(ScenarioError):
        profile_for_index(-1)


def test_contract_B_demand_manifest_records_identity(tmp_path):
    routes = write_demand(tmp_path / "m", seed=0, profile=DemandProfile.ASYMMETRIC, horizon_s=5)
    manifest = json.loads((routes.parent / "demand.manifest.json").read_text())
    assert manifest["profile"] == "asymmetric"
    assert manifest["traffic_seed"] == 0
    assert manifest["horizon_s"] == 5
    assert manifest["generator_version"]
    assert manifest["routes_sha256"]
    assert [v["vehicle_id"] for v in manifest["scheduled_vehicles"]] == ["veh0", "veh1", "veh2"]
    assert manifest["scheduled_vehicle_count"] == 3
