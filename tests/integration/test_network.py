"""Build contract Sections B and J: generated network and scenario, checked with real SUMO."""

import json

import pytest
import traci

from traffic_cps.cli import main
from traffic_cps.scenario import (
    INCOMING_LANES,
    ScenarioError,
    build_network,
    validate_network,
)


@pytest.fixture(scope="module")
def network_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("net") / "single"
    build_network(out)
    return out


def test_contract_J_network_has_one_signalized_junction_and_exact_movements(network_dir):
    summary = validate_network(network_dir / "network.net.xml")
    assert summary["tls_ids"] == ["J0"]
    assert sorted(summary["movements"]) == [
        ("N_J_0", "J_S_0"), ("N_J_1", "J_S_1"), ("W_J_0", "J_E_0"), ("W_J_1", "J_E_1"),
    ]


def test_contract_J_incoming_lane_lengths_are_generated_and_positive(network_dir):
    manifest = json.loads((network_dir / "network.manifest.json").read_text())
    lengths = manifest["incoming_lane_lengths_m"]
    assert sorted(lengths) == sorted(INCOMING_LANES)
    for lane, length in lengths.items():
        # Junction geometry shortens lanes, so the coordinate length (250 m) is an upper bound.
        assert 0 < length <= 250.0, lane


def test_contract_B_network_manifest_records_hashes_and_toolchain(network_dir):
    manifest = json.loads((network_dir / "network.manifest.json").read_text())
    for name in ["nodes.nod.xml", "edges.edg.xml", "connections.con.xml", "network.net.xml"]:
        assert len(manifest["sha256"][name]) == 64
    assert manifest["netconvert_version"] == "1.24.0"


def test_contract_B_network_content_hash_is_stable_across_rebuilds(network_dir, tmp_path):
    from traffic_cps.scenario import network_content_sha256

    rebuilt = tmp_path / "rebuilt"
    manifest = build_network(rebuilt)
    original = json.loads((network_dir / "network.manifest.json").read_text())
    assert manifest["content_sha256"] == original["content_sha256"]
    assert network_content_sha256(rebuilt / "network.net.xml") == original["content_sha256"]
    for name in ["nodes.nod.xml", "edges.edg.xml", "connections.con.xml"]:
        assert manifest["sha256"][name] == original["sha256"][name]


def test_contract_B_build_refuses_existing_nonempty_directory(network_dir):
    with pytest.raises(ScenarioError):
        build_network(network_dir)


def test_contract_B_sumo_controlled_links_match_manifest_movements(network_dir, tmp_path):
    """Smoke: SUMO itself loads the network and J0 controls exactly the four movements."""
    assert main([
        "scenario", "demand", "--network", str(network_dir), "--seed", "0",
        "--profile", "balanced", "--horizon", "60", "--output", str(tmp_path / "smoke"),
    ]) == 0
    scenario = tmp_path / "smoke"
    traci.start([
        "sumo", "-n", str(scenario / "network.net.xml"), "-r", str(scenario / "demand.rou.xml"),
        "--step-length", "1", "--begin", "0", "--end", "61", "--seed", "0",
        "--time-to-teleport", "-1", "--no-step-log", "true",
    ], label="network-smoke")
    try:
        links = traci.trafficlight.getControlledLinks("J0")
        pairs = sorted({(link[0][0], link[0][1]) for link in links if link})
        assert pairs == [("N_J_0", "J_S_0"), ("N_J_1", "J_S_1"), ("W_J_0", "J_E_0"), ("W_J_1", "J_E_1")]
        for _ in range(10):
            traci.simulationStep()
        assert traci.simulation.getTime() == 10.0
    finally:
        traci.close()


def test_contract_B_scenario_manifest_links_network_and_demand(network_dir, tmp_path):
    out = tmp_path / "scn"
    assert main([
        "scenario", "demand", "--network", str(network_dir), "--seed", "1",
        "--profile", "asymmetric", "--horizon", "60", "--output", str(out),
    ]) == 0
    manifest = json.loads((out / "scenario.manifest.json").read_text())
    network_manifest = json.loads((network_dir / "network.manifest.json").read_text())
    assert manifest["network"]["sha256"]["network.net.xml"] == network_manifest["sha256"]["network.net.xml"]
    assert (out / "network.net.xml").read_bytes() == (network_dir / "network.net.xml").read_bytes()
    assert manifest["demand"]["profile"] == "asymmetric"
    assert manifest["demand"]["traffic_seed"] == 1
