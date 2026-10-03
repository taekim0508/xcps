"""Command-line interface (build contract, Section I).

Exit codes: 0 success; 2 invalid configuration, resume, or environment;
1 runtime, safety, or persistence failure.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from traffic_cps import PYTHON_MAJOR_MINOR, SUMO_VERSION
from traffic_cps.config import Config, ConfigError, load_config
from traffic_cps.contracts import CompletionStatus
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


def cmd_saturation_check(args: argparse.Namespace) -> int:
    from traffic_cps.experiments import ExperimentError, saturation_check

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID
    try:
        record = saturation_check(config, args.network, args.seed, args.output, horizon_s=args.horizon)
    except (ExperimentError, ScenarioError) as exc:
        print(f"saturation check: FAIL {exc}", file=sys.stderr)
        return EXIT_FAILURE
    verdict = "PASS" if record["passed"] else "FAIL (lower peak demand, record the change, regenerate)"
    print(f"saturation check: {verdict}: {record['both_scores_at_cap']}/{record['free_choice_checks_in_window']} "
          f"free-choice checks in the peak window had both scores at cap "
          f"({record['fraction']:.1%}; limit {record['max_fraction']:.0%}); run {record['run_id']}")
    return EXIT_OK if record["passed"] else EXIT_FAILURE


# --------------------------------------------------------------------------
# run / explain / report
# --------------------------------------------------------------------------

CONTROLLERS = ("fixed", "queue", "q_learning")


def _make_controller(name: str, config: Config):
    from traffic_cps.controllers.fixed import FixedTimeController
    from traffic_cps.controllers.queue import QueueController

    if name == "fixed":  # pre-tuning target from the config; tuned values arrive in chunk 7
        return FixedTimeController(config.signal.fixed_green_target_s)
    return {"queue": QueueController}[name]()


@contextmanager
def _interruptible(stop):
    """First Ctrl-C: stop after the current simulated second and save. Second Ctrl-C: default behavior."""
    def handler(signum, frame):
        stop.request("interrupt requested (Ctrl-C)")
        signal.signal(signal.SIGINT, previous)
        print("interrupt: stopping after the current simulated second and saving completed learning; "
              "press Ctrl-C again to abort immediately", file=sys.stderr)

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _notify(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _load_frozen_learner(config: Config, checkpoint: Path, scenario: Path, horizon: int):
    """Validate a checkpoint for frozen evaluation on this scenario before any traffic starts."""
    from traffic_cps.checkpoints import CheckpointStore, compatibility, file_sha256, learner_from_checkpoint
    from traffic_cps.scenario import SCENARIO_MANIFEST

    try:
        network = json.loads((scenario / SCENARIO_MANIFEST).read_text(encoding="utf-8"))["network"]
    except (OSError, ValueError, KeyError) as exc:
        raise ConfigError(f"cannot read the scenario's network identity: {exc}") from exc
    data = CheckpointStore(checkpoint.parent).load(checkpoint, compatibility(config, network, horizon), "evaluate")
    provenance = {"purpose": "frozen_evaluation", "checkpoint_path": str(checkpoint),
                  "checkpoint_id": data["checkpoint_id"], "checkpoint_file_sha256": file_sha256(checkpoint),
                  "integrity_sha256": data["integrity_sha256"]}
    return learner_from_checkpoint(config, data), provenance


def cmd_run(args: argparse.Namespace) -> int:
    from traffic_cps.checkpoints import CheckpointError
    from traffic_cps.contracts import Mode
    from traffic_cps.report import build_report
    from traffic_cps.runtime import Runtime, StopRequest

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID
    if (args.controller == "q_learning") != (args.checkpoint is not None):
        print("run: FAIL --checkpoint is required for q_learning and only valid with it", file=sys.stderr)
        return EXIT_INVALID
    horizon = config.simulation.horizon_s if args.horizon is None else args.horizon
    learner, provenance = None, None
    try:
        if args.controller == "q_learning":
            from traffic_cps.controllers.q_learning import QLearningController

            learner, provenance = _load_frozen_learner(config, args.checkpoint, args.scenario, horizon)
            controller = QLearningController(learner)
        else:
            controller = _make_controller(args.controller, config)
    except (CheckpointError, ConfigError) as exc:
        print(f"run: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID

    stop = StopRequest()
    try:
        runtime = Runtime(config, args.scenario, controller, args.output, horizon_s=horizon, notify=_notify,
                          mode=Mode.EVALUATE, learner=learner, stop=stop, provenance=provenance)
        with _interruptible(stop):
            result = runtime.run()
    except Exception as exc:  # setup failures before the run loop (e.g. output exists)
        print(f"run: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INVALID
    print(f"run {result.run_id}: {result.completion.value} (final valid time {result.final_valid_time_s}s "
          f"of {result.planned_horizon_s}s); log persisted: {result.persistence_ok}")
    print(f"run directory: {result.run_dir}")
    if result.failure:
        print(f"failure: {result.failure}", file=sys.stderr)
    report_ok = True
    try:
        print(f"report: {build_report(result.run_dir)}")
    except Exception as exc:
        report_ok = False
        print(f"report: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
    ok = result.completion is CompletionStatus.COMPLETED and result.persistence_ok and report_ok
    return EXIT_OK if ok else EXIT_FAILURE


def cmd_train(args: argparse.Namespace) -> int:
    from traffic_cps.checkpoints import CheckpointError
    from traffic_cps.report import build_report
    from traffic_cps.runtime import StopRequest, train

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID
    if args.resume is None:
        if args.output is None or args.additional_episodes is not None:
            print("train: FAIL fresh training needs --output (and optionally --episodes)", file=sys.stderr)
            return EXIT_INVALID
        episodes = config.experiment.training_episodes if args.episodes is None else args.episodes
    elif args.output is not None or args.episodes is not None or args.additional_episodes is None:
        print("train: FAIL resume needs --resume and --additional-episodes only; it continues in the "
              "checkpoint's training folder", file=sys.stderr)
        return EXIT_INVALID
    reports_ok = True

    def on_episode(result) -> None:
        nonlocal reports_ok
        c = result.counters
        print(f"{result.run_id}: {result.completion.value}; updates {c['learning_updates']}, discarded "
              f"{c['feedback_discarded']}, accepted choices {c['accepted_requests']}; log persisted: "
              f"{result.persistence_ok}; checkpoint: {result.checkpoint_status} ({result.last_checkpoint_id})")
        if result.failure:
            print(f"failure: {result.failure}", file=sys.stderr)
        try:
            build_report(result.run_dir)
        except Exception as exc:
            reports_ok = False
            print(f"report: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)

    stop = StopRequest()
    try:
        with _interruptible(stop):
            if args.resume is None:
                outcome = train(config, episodes, args.output, horizon_s=args.horizon, notify=_notify,
                                stop=stop, on_episode=on_episode)
            else:
                outcome = train(config, horizon_s=args.horizon, notify=_notify, resume=args.resume,
                                additional_episodes=args.additional_episodes, stop=stop, on_episode=on_episode)
    except (CheckpointError, ConfigError, ValueError, FileExistsError) as exc:  # before any traffic
        print(f"train: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except Exception as exc:
        print(f"train: FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    lr = outcome.learner
    print(f"training {outcome.training_dir.name}: {outcome.completed_episodes}/{outcome.target_completed} "
          f"episodes completed; policy revision {lr.table.revision}; {len(lr.table)} states; epsilon "
          f"{lr.schedule.epsilon:.4f} after {lr.schedule.accepted_choices} accepted choices")
    print(f"latest checkpoint: {outcome.training_dir / 'checkpoints' / 'training_latest.json'}")
    for d in outcome.development:
        print(f"development evaluation episode {d['episode']} seed {d['seed']}: "
              f"{'completed' if d['ok'] else 'FAILED'} ({d['run_id']})")
    if outcome.stopped:
        print(f"stopped: {outcome.stopped}", file=sys.stderr)
    if outcome.persistence_failure:
        print(f"persistence failure: {outcome.persistence_failure}", file=sys.stderr)
    return EXIT_OK if outcome.ok and reports_ok else EXIT_FAILURE


def cmd_explain(args: argparse.Namespace) -> int:
    from traffic_cps.explain import ExplainError, explain, render_text

    try:
        print(render_text(explain(args.run, args.decision)))
    except ExplainError as exc:
        print(f"explain: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    from traffic_cps.explain import ExplainError
    from traffic_cps.report import build_report

    try:
        print(f"report: {build_report(args.run)}")
    except ExplainError as exc:
        print(f"report: FAIL {exc}", file=sys.stderr)
        return EXIT_INVALID
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

    sat = scenario_sub.add_parser("saturation-check", help="peak saturation check with the queue controller")
    sat.add_argument("--config", type=Path, required=True)
    sat.add_argument("--network", type=Path, required=True)
    sat.add_argument("--seed", type=int, required=True)
    sat.add_argument("--output", type=Path, required=True)
    sat.add_argument("--horizon", type=int, help="override the configured horizon")
    sat.set_defaults(func=cmd_saturation_check)

    run = sub.add_parser("run", help="run one baseline controller on a scenario")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--controller", choices=CONTROLLERS, required=True)
    run.add_argument("--scenario", type=Path, required=True)
    run.add_argument("--horizon", type=int, help="override the configured horizon (must match the scenario)")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--checkpoint", type=Path, help="frozen Q-learning checkpoint (q_learning only)")
    run.set_defaults(func=cmd_run)

    tr = sub.add_parser("train", help="train the tabular Q-learning controller")
    tr.add_argument("--config", type=Path, required=True)
    tr.add_argument("--episodes", type=int, help="fresh: number of episodes (default: experiment.training_episodes)")
    tr.add_argument("--horizon", type=int, help="override the configured horizon (must match on resume)")
    tr.add_argument("--output", type=Path, help="fresh: new training folder")
    tr.add_argument("--resume", type=Path, help="resume from this explicitly selected checkpoint")
    tr.add_argument("--additional-episodes", type=int, help="resume: episodes to add")
    tr.set_defaults(func=cmd_train)

    exp = sub.add_parser("explain", help="explain one recorded decision or event")
    exp.add_argument("--run", type=Path, required=True)
    exp.add_argument("--decision", required=True, help="event ID")
    exp.set_defaults(func=cmd_explain)

    rep = sub.add_parser("report", help="write report.html for a finished run (never launches SUMO)")
    rep.add_argument("--run", type=Path, required=True)
    rep.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage errors exit 2 already
        return int(exc.code) if exc.code is not None else EXIT_INVALID
    return args.func(args)
