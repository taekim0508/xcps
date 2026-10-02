"""Command-line interface (build contract, Section I).

Exit codes: 0 success; 2 invalid configuration, resume, or environment;
1 runtime, safety, or persistence failure.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import shutil
import subprocess
import sys
from pathlib import Path

from traffic_cps import PYTHON_MAJOR_MINOR, SUMO_VERSION
from traffic_cps.config import ConfigError, load_config
from traffic_cps.scenario import (
    DemandProfile,
    ScenarioError,
    build_network,
    create_scenario,
    parse_sumo_version,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INVALID = 2

PYTHON_CLIENTS = ("traci", "sumolib", "eclipse-sumo")
_MISSING_LIB_RE = re.compile(
    r"(?:error while loading shared libraries: (\S+?):|Library not loaded: (\S+))"
)

__all__ = ["main", "DoctorError", "check_binary", "check_python_clients", "parse_sumo_version"]


class DoctorError(RuntimeError):
    """The simulator toolchain is missing or does not match the pinned versions."""


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def check_binary(name: str, explicit_path: str | None, expected: str) -> tuple[str, str]:
    """Resolve a SUMO tool (explicit path first, then PATH) and verify its version."""
    path = explicit_path or shutil.which(name)
    if path is None:
        raise DoctorError(f"{name} not found on PATH; pass --{name} or install eclipse-sumo=={expected}")
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
    except OSError as exc:
        raise DoctorError(f"{name} at {path} cannot be executed: {exc}") from exc
    if proc.returncode != 0:
        missing = _MISSING_LIB_RE.search(proc.stderr)
        if missing:
            lib = missing.group(1) or missing.group(2)
            raise DoctorError(
                f"{name} at {path} cannot load native library {lib}. Install the system package that "
                f"provides it (see SUMO installation guidance), or run inside the project container (./dev.sh)."
            )
        raise DoctorError(f"{name} at {path} failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
    version = parse_sumo_version(proc.stdout)
    if version is None:
        raise DoctorError(f"{name} at {path}: cannot parse version from {proc.stdout.strip()[:200]!r}")
    if version != expected:
        raise DoctorError(f"{name} at {path} reports {version}; pinned toolchain requires {expected}")
    return version, path


def check_python_clients(expected: str) -> dict[str, str]:
    """Python interpreter and SUMO client packages must match the pinned versions."""
    if sys.version_info[:2] != PYTHON_MAJOR_MINOR:
        raise DoctorError(f"Python {sys.version.split()[0]} found; {'.'.join(map(str, PYTHON_MAJOR_MINOR))} required")
    versions = {}
    for package in PYTHON_CLIENTS:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise DoctorError(f"Python package {package}=={expected} is not installed") from exc
        if versions[package] != expected:
            raise DoctorError(f"Python package {package} is {versions[package]}; {expected} required")
    return versions


def cmd_doctor(args: argparse.Namespace) -> int:
    if args.config:
        try:
            load_config(args.config)
        except ConfigError as exc:
            print(f"config: FAIL {exc}", file=sys.stderr)
            return EXIT_INVALID
        print(f"config: ok ({args.config})")
    try:
        for name, explicit in (("sumo", args.sumo), ("netconvert", args.netconvert)):
            version, path = check_binary(name, explicit, SUMO_VERSION)
            print(f"{name}: ok {version} ({path})")
        for package, version in check_python_clients(SUMO_VERSION).items():
            print(f"{package}: ok {version}")
    except DoctorError as exc:
        print(f"doctor: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID
    print(f"python: ok {sys.version.split()[0]}")
    print("doctor: all checks passed")
    return EXIT_OK


# --------------------------------------------------------------------------
# scenario
# --------------------------------------------------------------------------

def cmd_scenario_build(args: argparse.Namespace) -> int:
    try:
        manifest = build_network(args.output, netconvert=args.netconvert or "netconvert")
    except ScenarioError as exc:
        print(f"scenario build: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID
    lengths = ", ".join(f"{k}={v:.2f}m" for k, v in sorted(manifest["incoming_lane_lengths_m"].items()))
    print(f"scenario build: wrote {args.output} (netconvert {manifest['netconvert_version']}; {lengths})")
    return EXIT_OK


def cmd_scenario_demand(args: argparse.Namespace) -> int:
    try:
        manifest = create_scenario(args.network, args.output, args.seed, DemandProfile(args.profile), args.horizon)
    except ScenarioError as exc:
        print(f"scenario demand: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID
    demand = manifest["demand"]
    print(
        f"scenario demand: wrote {args.output} (profile {demand['profile']}, seed {demand['traffic_seed']}, "
        f"horizon {demand['horizon_s']}s, {demand['scheduled_vehicle_count']} scheduled vehicles)"
    )
    return EXIT_OK


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traffic-cps", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify the pinned simulator toolchain")
    doctor.add_argument("--config", type=Path, help="also validate this configuration file")
    doctor.add_argument("--sumo", help="explicit path to the sumo binary (default: PATH)")
    doctor.add_argument("--netconvert", help="explicit path to the netconvert binary (default: PATH)")
    doctor.set_defaults(func=cmd_doctor)

    scenario = sub.add_parser("scenario", help="generate network and demand")
    scenario_sub = scenario.add_subparsers(dest="scenario_command", required=True)

    build = scenario_sub.add_parser("build", help="generate the intersection network with netconvert")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--netconvert", help="explicit path to netconvert (default: PATH)")
    build.set_defaults(func=cmd_scenario_build)

    demand = scenario_sub.add_parser("demand", help="materialize vehicle demand for one seed")
    demand.add_argument("--network", type=Path, required=True)
    demand.add_argument("--seed", type=int, required=True)
    demand.add_argument("--profile", choices=[p.value for p in DemandProfile], required=True)
    demand.add_argument("--horizon", type=int, required=True)
    demand.add_argument("--output", type=Path, required=True)
    demand.set_defaults(func=cmd_scenario_demand)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage errors exit 2 already
        return int(exc.code) if exc.code is not None else EXIT_INVALID
    return args.func(args)
