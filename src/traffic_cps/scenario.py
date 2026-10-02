"""Generate the intersection network and vehicle demand (build contract, Section B).

All XML is produced by string formatting in a fixed order so that the same
inputs always yield byte-identical files. `network.net.xml` is never
hand-authored: it is produced by netconvert from the plain XML written here.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

import sumolib

from traffic_cps import SUMO_VERSION
from traffic_cps.config import canonical_json, sha256_file, sha256_text

GENERATOR_VERSION = "demand_v1"
TLS_ID = "J0"
INCOMING_LANES = ("N_J_0", "N_J_1", "W_J_0", "W_J_1")
EXPECTED_MOVEMENTS = (
    ("N_J_0", "J_S_0"),
    ("N_J_1", "J_S_1"),
    ("W_J_0", "J_E_0"),
    ("W_J_1", "J_E_1"),
)
ROUTES = {"NS": ("N_J", "J_S"), "WE": ("W_J", "J_E")}
NETWORK_SOURCE_NAMES = ("nodes.nod.xml", "edges.edg.xml", "connections.con.xml")
NETWORK_FILE = "network.net.xml"
NETWORK_MANIFEST = "network.manifest.json"
ROUTES_FILE = "demand.rou.xml"
DEMAND_MANIFEST = "demand.manifest.json"
SCENARIO_MANIFEST = "scenario.manifest.json"

# Vehicle type `car` (Section B). Unspecified dynamics keep pinned SUMO defaults.
VEHICLE_TYPE = (
    ("id", "car"),
    ("vClass", "passenger"),
    ("length", 5.0),
    ("minGap", 2.5),
    ("maxSpeed", 13.9),
    ("accel", 2.6),
    ("decel", 4.5),
    ("tau", 1.0),
    ("sigma", 0.5),
    ("carFollowModel", "Krauss"),
    ("speedFactor", 1.0),
    ("speedDev", 0.0),
)


class ScenarioError(RuntimeError):
    """Scenario generation or validation failed."""


class DemandProfile(str, Enum):
    BALANCED = "balanced"
    ASYMMETRIC = "asymmetric"
    PEAK = "peak"


PROFILE_CYCLE = (DemandProfile.BALANCED, DemandProfile.ASYMMETRIC, DemandProfile.PEAK)


@dataclass(frozen=True)
class ScheduledVehicle:
    vehicle_id: str
    depart_s: int
    route: str
    depart_lane: int


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

def _fmt(value) -> str:
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def network_source_files() -> dict[str, str]:
    """Plain-XML inputs for netconvert, keyed by file name."""
    nodes = [("J0", 0.0, 0.0, "traffic_light"), ("N", 0.0, 250.0, "priority"),
             ("S", 0.0, -250.0, "priority"), ("W", -250.0, 0.0, "priority"),
             ("E", 250.0, 0.0, "priority")]
    edges = [("N_J", "N", "J0"), ("J_S", "J0", "S"), ("W_J", "W", "J0"), ("J_E", "J0", "E")]
    connections = [("N_J", "J_S", 0), ("N_J", "J_S", 1), ("W_J", "J_E", 0), ("W_J", "J_E", 1)]

    node_lines = [f'    <node id="{i}" x="{_fmt(x)}" y="{_fmt(y)}" type="{t}"/>' for i, x, y, t in nodes]
    edge_lines = [
        f'    <edge id="{i}" from="{f}" to="{t}" priority="1" numLanes="2" speed="{_fmt(13.9)}"/>'
        for i, f, t in edges
    ]
    conn_lines = [
        f'    <connection from="{f}" to="{t}" fromLane="{lane}" toLane="{lane}"/>' for f, t, lane in connections
    ]
    return {
        "nodes.nod.xml": "<nodes>\n" + "\n".join(node_lines) + "\n</nodes>\n",
        "edges.edg.xml": "<edges>\n" + "\n".join(edge_lines) + "\n</edges>\n",
        "connections.con.xml": "<connections>\n" + "\n".join(conn_lines) + "\n</connections>\n",
    }


def _prepare_output(out_dir: Path) -> None:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ScenarioError(f"output directory {out_dir} already exists and is not empty; refusing to overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)


_VERSION_RE = re.compile(r"^Eclipse SUMO \S+ Version (\d+\.\d+\.\d+)\s*$", re.MULTILINE)


def parse_sumo_version(text: str) -> str | None:
    """Release version from `<tool> --version` output, or None if not a plain release."""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def tool_version(binary: str) -> str:
    """Version reported by a SUMO tool (`sumo` or `netconvert`)."""
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60)
    except OSError as exc:
        raise ScenarioError(f"cannot run {binary}: {exc}") from exc
    version = parse_sumo_version(proc.stdout)
    if proc.returncode != 0 or version is None:
        raise ScenarioError(f"{binary} --version failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return version


def build_network(out_dir: Path | str, netconvert: str = "netconvert") -> dict:
    """Write plain XML, run netconvert, validate, and write network.manifest.json."""
    out_dir = Path(out_dir)
    _prepare_output(out_dir)
    version = tool_version(netconvert)
    if version != SUMO_VERSION:
        raise ScenarioError(f"netconvert {version} does not match pinned {SUMO_VERSION}")

    for name, text in network_source_files().items():
        (out_dir / name).write_text(text, encoding="utf-8")

    # Run inside out_dir with relative names so netconvert's header records no host paths.
    cmd = [
        netconvert,
        "--node-files", "nodes.nod.xml",
        "--edge-files", "edges.edg.xml",
        "--connection-files", "connections.con.xml",
        "--no-turnarounds", "true",
        "--output-file", NETWORK_FILE,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=out_dir)
    (out_dir / "netconvert.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise ScenarioError(f"netconvert failed (exit {proc.returncode}); see {out_dir / 'netconvert.log'}")

    summary = validate_network(out_dir / NETWORK_FILE)
    manifest = {
        "tls_id": TLS_ID,
        "movements": [list(m) for m in EXPECTED_MOVEMENTS],
        "incoming_lane_lengths_m": summary["incoming_lane_lengths_m"],
        "netconvert_version": version,
        "netconvert_command": [Path(c).name if i == 0 else c for i, c in enumerate(cmd)],
        "sha256": {name: sha256_file(out_dir / name) for name in (*NETWORK_SOURCE_NAMES, NETWORK_FILE)},
        "content_sha256": network_content_sha256(out_dir / NETWORK_FILE),
    }
    _write_json(out_dir / NETWORK_MANIFEST, manifest)
    return manifest


_HEADER_RE = re.compile(r"\A(<\?xml[^>]*\?>\s*)?<!--.*?-->\s*", re.DOTALL)


def network_content_sha256(net_path: Path | str) -> str:
    """Hash of the network with netconvert's leading comment header removed.

    netconvert stamps a generation time into that header, so the raw file hash
    changes on every rebuild. This content hash is the network identity used
    for compatibility; the raw hash still proves a copy is byte-identical.
    """
    text = Path(net_path).read_text(encoding="utf-8")
    match = _HEADER_RE.match(text)
    if match is None:
        raise ScenarioError(f"{net_path}: expected a leading netconvert comment header")
    return sha256_text((match.group(1) or "") + text[match.end():])


def validate_network(net_path: Path | str) -> dict:
    """Check one signalized junction J0, exactly the four movements, and positive lane lengths."""
    try:
        net = sumolib.net.readNet(str(net_path))
    except Exception as exc:  # sumolib raises assorted parser errors
        raise ScenarioError(f"cannot read network {net_path}: {exc}") from exc

    tls_ids = sorted(tls.getID() for tls in net.getTrafficLights())
    if tls_ids != [TLS_ID]:
        raise ScenarioError(f"expected exactly one signalized junction {TLS_ID}, found {tls_ids}")

    movements = sorted(
        (conn.getFromLane().getID(), conn.getToLane().getID())
        for edge in net.getEdges()
        for lane in edge.getLanes()
        for conn in lane.getOutgoing()
    )
    if movements != sorted(EXPECTED_MOVEMENTS):
        raise ScenarioError(f"unexpected movements {movements}; expected {sorted(EXPECTED_MOVEMENTS)}")

    controlled = sorted({(c[0].getID(), c[1].getID()) for c in net.getTLS(TLS_ID).getConnections()})
    if controlled != sorted(EXPECTED_MOVEMENTS):
        raise ScenarioError(f"{TLS_ID} controls {controlled}; expected {sorted(EXPECTED_MOVEMENTS)}")

    lengths = {}
    for lane_id in INCOMING_LANES:
        length = net.getLane(lane_id).getLength()
        if not length > 0:
            raise ScenarioError(f"incoming lane {lane_id} has nonpositive length {length}")
        lengths[lane_id] = length
    return {"tls_ids": tls_ids, "movements": movements, "incoming_lane_lengths_m": lengths}


# --------------------------------------------------------------------------
# Demand
# --------------------------------------------------------------------------

def profile_for_index(index: int) -> DemandProfile:
    """Profile for the index-th seed in an ordered train/development/final list."""
    if index < 0:
        raise ScenarioError(f"seed index must be nonnegative, got {index}")
    return PROFILE_CYCLE[index % len(PROFILE_CYCLE)]


def direction_probabilities(profile: DemandProfile, t: int, horizon_s: int) -> tuple[float, float]:
    """Per-second insertion probability (NS, WE) at integer second t."""
    if profile is DemandProfile.BALANCED:
        return (0.30, 0.30)
    if profile is DemandProfile.ASYMMETRIC:
        return (0.20, 0.50)
    if profile is DemandProfile.PEAK:
        # Integer comparisons avoid float error at the H/3 and 2H/3 boundaries.
        if 3 * t < horizon_s:
            p = 0.15
        elif 3 * t < 2 * horizon_s:
            p = 0.45
        else:
            p = 0.20
        return (p, p)
    raise ScenarioError(f"unknown profile {profile!r}")


def generate_demand(seed: int, profile: DemandProfile, horizon_s: int) -> list[ScheduledVehicle]:
    """Materialize individual departures with a dedicated Random(seed).

    Each second draws exactly one uniform for NS then one for WE, whether or
    not a vehicle is inserted, so the draw sequence never depends on outcomes.
    """
    if not isinstance(horizon_s, int) or horizon_s <= 0 or horizon_s % 5 != 0:
        raise ScenarioError(f"horizon must be a positive integer divisible by 5, got {horizon_s!r}")
    profile = DemandProfile(profile)
    rng = random.Random(seed)
    next_lane = {"NS": 0, "WE": 0}
    vehicles: list[ScheduledVehicle] = []
    for t in range(horizon_s):
        p_ns, p_we = direction_probabilities(profile, t, horizon_s)
        draws = (("NS", rng.random(), p_ns), ("WE", rng.random(), p_we))
        for route, draw, probability in draws:
            if draw < probability:
                vehicles.append(ScheduledVehicle(f"veh{len(vehicles)}", t, route, next_lane[route]))
                next_lane[route] = 1 - next_lane[route]
    return vehicles


def routes_xml(vehicles: list[ScheduledVehicle]) -> str:
    vtype = " ".join(f'{k}="{_fmt(v)}"' for k, v in VEHICLE_TYPE)
    lines = ["<routes>", f"    <vType {vtype}/>"]
    for route_id, edges in ROUTES.items():
        lines.append(f'    <route id="{route_id}" edges="{" ".join(edges)}"/>')
    for v in vehicles:
        lines.append(
            f'    <vehicle id="{v.vehicle_id}" type="car" route="{v.route}" depart="{v.depart_s}" '
            f'departLane="{v.depart_lane}" departPos="base" departSpeed="0"/>'
        )
    lines.append("</routes>")
    return "\n".join(lines) + "\n"


def write_demand(out_dir: Path | str, seed: int, profile: DemandProfile, horizon_s: int) -> Path:
    """Write demand.rou.xml and demand.manifest.json into a new directory."""
    out_dir = Path(out_dir)
    vehicles = generate_demand(seed, profile, horizon_s)
    _prepare_output(out_dir)
    routes_path = out_dir / ROUTES_FILE
    routes_path.write_text(routes_xml(vehicles), encoding="utf-8")
    probabilities = sorted({direction_probabilities(DemandProfile(profile), t, horizon_s) for t in range(horizon_s)})
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "profile": DemandProfile(profile).value,
        "traffic_seed": seed,
        "horizon_s": horizon_s,
        "probabilities_ns_we": [list(p) for p in probabilities],
        "scheduled_vehicle_count": len(vehicles),
        "scheduled_vehicles": [asdict(v) for v in vehicles],
        "routes_sha256": sha256_file(routes_path),
    }
    _write_json(out_dir / DEMAND_MANIFEST, manifest)
    return routes_path


def create_scenario(network_dir: Path | str, out_dir: Path | str, seed: int,
                    profile: DemandProfile, horizon_s: int) -> dict:
    """Demand plus a byte-for-byte network copy and a linking scenario.manifest.json."""
    network_dir, out_dir = Path(network_dir), Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ScenarioError(f"output directory {out_dir} already exists and is not empty; refusing to overwrite")
    network_manifest = _read_network_manifest(network_dir)

    write_demand(out_dir, seed, profile, horizon_s)
    shutil.copyfile(network_dir / NETWORK_FILE, out_dir / NETWORK_FILE)
    if sha256_file(out_dir / NETWORK_FILE) != network_manifest["sha256"][NETWORK_FILE]:
        raise ScenarioError("network copy does not match its manifest hash")
    demand_manifest = json.loads((out_dir / DEMAND_MANIFEST).read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 1,
        "network": network_manifest,
        "demand": demand_manifest,
        "sha256": {
            NETWORK_FILE: sha256_file(out_dir / NETWORK_FILE),
            ROUTES_FILE: sha256_file(out_dir / ROUTES_FILE),
            DEMAND_MANIFEST: sha256_file(out_dir / DEMAND_MANIFEST),
        },
        "calibration": [],  # peak saturation check results and any probability changes (chunk 2)
    }
    _write_json(out_dir / SCENARIO_MANIFEST, manifest)
    return manifest


def _read_network_manifest(network_dir: Path) -> dict:
    path = network_dir / NETWORK_MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"missing or unreadable network manifest {path}: {exc}") from exc
    actual = sha256_file(network_dir / NETWORK_FILE) if (network_dir / NETWORK_FILE).is_file() else None
    if actual != manifest.get("sha256", {}).get(NETWORK_FILE):
        raise ScenarioError(f"{network_dir / NETWORK_FILE} is missing or does not match its manifest")
    return manifest


def _write_json(path: Path, data: dict) -> None:
    # Validate strictness via canonical_json, then write a readable form.
    canonical_json(data)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
