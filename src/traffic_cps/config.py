"""Safe YAML loading, strict validation, and digests (build contract, Section C).

Every key is required and every unknown key is rejected: a typo must fail
loudly rather than fall back to a default the user never chose.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from traffic_cps import SUMO_VERSION
from traffic_cps.contracts import Direction

SCHEMA_VERSION = 1
LANES_PER_DIRECTION = 2


class ConfigError(ValueError):
    """Invalid or unsupported configuration (CLI exit code 2)."""


# --------------------------------------------------------------------------
# Canonical JSON and hashing (shared by manifests and checkpoints)
# --------------------------------------------------------------------------

def canonical_json(data: Any) -> str:
    """Sorted keys, compact separators, finite numbers only."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Typed configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SimulationConfig:
    horizon_s: int
    step_s: int
    control_interval_s: int
    tls_id: str
    initial_green: Direction
    gui: bool
    sumo_version: str


@dataclass(frozen=True)
class NetworkConfig:
    directory: Path
    lanes: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class SignalConfig:
    yellow_s: int
    minimum_green_s: int
    green_service_limit_s: int
    all_red_s: int
    fixed_green_target_s: int
    fixed_green_candidates_s: tuple[int, ...]


@dataclass(frozen=True)
class SensorConfig:
    vehicle_length_m: float
    minimum_gap_m: float
    retries: int


@dataclass(frozen=True)
class LearningConfig:
    alpha: float
    discount_factor: float
    discount_unit_s: int
    epsilon_initial: float
    epsilon_floor: float
    epsilon_decay: float
    controller_seed: int
    queue_edges: tuple[float, ...]
    elapsed_edges_s: tuple[float, ...]
    tie_abs_tolerance: float
    evaluation_requires_both_actions_updated: bool


@dataclass(frozen=True)
class StorageConfig:
    output_root: Path
    checkpoint_every_updates: int
    keep_previous: int
    flush_each_tick: bool


@dataclass(frozen=True)
class ExperimentConfig:
    training_episodes: int
    training_seed_start: int
    development_seeds: tuple[int, ...]
    final_seeds: tuple[int, ...]
    development_after_episodes: tuple[int, ...]


@dataclass(frozen=True)
class FaultLevel:
    noise_bound_vehicles: int
    stale_probability: float


@dataclass(frozen=True)
class RobustnessConfig:
    levels: dict[str, FaultLevel]
    noise_seed_offset: int
    saturation_check_max_fraction: float


@dataclass(frozen=True)
class Config:
    schema_version: int
    simulation: SimulationConfig
    network: NetworkConfig
    signal: SignalConfig
    sensor: SensorConfig
    learning: LearningConfig
    storage: StorageConfig
    experiment: ExperimentConfig
    robustness: RobustnessConfig
    source_path: Path
    project_root: Path

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready resolved configuration (for config.resolved.json)."""
        data = asdict(self)
        return _jsonable(data)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Direction):
        return value.value
    return value


def config_digest(config: Config) -> str:
    """SHA-256 of the resolved configuration's canonical JSON."""
    return sha256_text(canonical_json(config.to_dict()))


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def find_project_root(start: Path) -> Path:
    """Nearest ancestor directory containing pyproject.toml."""
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError(f"cannot locate project root (no pyproject.toml above {start})")


def load_config(path: Path | str) -> Config:
    path = Path(path).resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        raw = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - strict SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    root = find_project_root(path.parent)
    return _build(raw, path, root)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

class _Section:
    """Reads required keys from one mapping and rejects any left unread."""

    def __init__(self, data: Any, where: str):
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
        self._data = data
        self._where = where
        self._read: set[str] = set()

    def _get(self, key: str) -> Any:
        if key not in self._data:
            raise ConfigError(f"{self._where}.{key}: required key is missing")
        self._read.add(key)
        return self._data[key]

    def name(self, key: str) -> str:
        return f"{self._where}.{key}"

    def section(self, key: str) -> "_Section":
        return _Section(self._get(key), self.name(key))

    def raw(self, key: str) -> Any:
        return self._get(key)

    def int(self, key: str) -> int:
        return _as_int(self._get(key), self.name(key))

    def float(self, key: str) -> float:
        return _as_float(self._get(key), self.name(key))

    def bool(self, key: str) -> bool:
        value = self._get(key)
        if not isinstance(value, bool):
            raise ConfigError(f"{self.name(key)}: expected true/false, got {value!r}")
        return value

    def str(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{self.name(key)}: expected a non-empty string, got {value!r}")
        return value

    def int_list(self, key: str) -> tuple[int, ...]:
        return tuple(_as_int(v, f"{self.name(key)}[{i}]") for i, v in enumerate(self._list(key)))

    def float_list(self, key: str) -> tuple[float, ...]:
        return tuple(_as_float(v, f"{self.name(key)}[{i}]") for i, v in enumerate(self._list(key)))

    def _list(self, key: str) -> list:
        value = self._get(key)
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{self.name(key)}: expected a non-empty list, got {value!r}")
        return value

    def done(self) -> None:
        unknown = sorted(set(self._data) - self._read, key=str)
        if unknown:
            raise ConfigError(f"{self._where}: unknown key(s) {unknown}")


def _as_int(value: Any, where: str) -> int:
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    return value


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ConfigError(f"{where}: must be finite, got {value!r}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _probability(value: float, where: str) -> None:
    _require(0.0 <= value <= 1.0, f"{where}: probability must be in [0, 1], got {value}")


def _strictly_increasing(values: tuple, where: str) -> None:
    _require(all(a < b for a, b in zip(values, values[1:])), f"{where}: must be strictly increasing")


def _build(raw: Any, source: Path, root: Path) -> Config:
    top = _Section(raw, "config")

    schema_version = top.int("schema_version")
    _require(schema_version == SCHEMA_VERSION, f"config.schema_version: unsupported {schema_version}")

    sim = top.section("simulation")
    simulation = SimulationConfig(
        horizon_s=sim.int("horizon_s"),
        step_s=sim.int("step_s"),
        control_interval_s=sim.int("control_interval_s"),
        tls_id=sim.str("tls_id"),
        initial_green=_direction(sim.str("initial_green"), sim.name("initial_green")),
        gui=sim.bool("gui"),
        sumo_version=sim.str("sumo_version"),
    )
    sim.done()
    _require(simulation.step_s == 1, "simulation.step_s: only 1-second steps are supported")
    _require(simulation.control_interval_s > 0, "simulation.control_interval_s: must be positive")
    _require(simulation.horizon_s > 0 and simulation.horizon_s % 5 == 0,
             "simulation.horizon_s: must be a positive integer divisible by 5")
    _require(simulation.horizon_s % simulation.control_interval_s == 0,
             "simulation.horizon_s: must be divisible by control_interval_s")
    _require(simulation.sumo_version == SUMO_VERSION,
             f"simulation.sumo_version: pinned toolchain is {SUMO_VERSION}, got {simulation.sumo_version}")

    net = top.section("network")
    directory = net.str("directory")
    lanes_section = net.section("lanes")
    lanes = {d.value: _lane_list(lanes_section.raw(d.value), lanes_section.name(d.value)) for d in Direction}
    lanes_section.done()
    net.done()
    all_lanes = [lane for group in lanes.values() for lane in group]
    _require(len(all_lanes) == len(set(all_lanes)), f"network.lanes: duplicate lane IDs {all_lanes}")
    network = NetworkConfig(directory=_resolve(root, directory), lanes=lanes)

    sig = top.section("signal")
    signal = SignalConfig(
        yellow_s=sig.int("yellow_s"),
        minimum_green_s=sig.int("minimum_green_s"),
        green_service_limit_s=sig.int("green_service_limit_s"),
        all_red_s=sig.int("all_red_s"),
        fixed_green_target_s=sig.int("fixed_green_target_s"),
        fixed_green_candidates_s=sig.int_list("fixed_green_candidates_s"),
    )
    sig.done()
    for key in ("yellow_s", "minimum_green_s", "green_service_limit_s", "fixed_green_target_s"):
        _require(getattr(signal, key) > 0, f"signal.{key}: must be positive")
    _require(signal.minimum_green_s < signal.green_service_limit_s,
             "signal.minimum_green_s: must be below green_service_limit_s")
    _require(signal.all_red_s == 0, "signal.all_red_s: all-red clearance is not supported in this version")
    limit = signal.green_service_limit_s
    _require(signal.fixed_green_target_s < limit,
             f"signal.fixed_green_target_s: must be strictly below green_service_limit_s ({limit})")
    for c in signal.fixed_green_candidates_s:
        _require(0 < c < limit, f"signal.fixed_green_candidates_s: {c} must be in (0, {limit})")
    _require(len(set(signal.fixed_green_candidates_s)) == len(signal.fixed_green_candidates_s),
             "signal.fixed_green_candidates_s: duplicates")

    sen = top.section("sensor")
    sensor = SensorConfig(
        vehicle_length_m=sen.float("vehicle_length_m"),
        minimum_gap_m=sen.float("minimum_gap_m"),
        retries=sen.int("retries"),
    )
    sen.done()
    _require(sensor.vehicle_length_m > 0, "sensor.vehicle_length_m: must be positive")
    _require(sensor.minimum_gap_m >= 0, "sensor.minimum_gap_m: must be nonnegative")
    _require(sensor.retries >= 0, "sensor.retries: must be nonnegative")

    lrn = top.section("learning")
    learning = LearningConfig(
        alpha=lrn.float("alpha"),
        discount_factor=lrn.float("discount_factor"),
        discount_unit_s=lrn.int("discount_unit_s"),
        epsilon_initial=lrn.float("epsilon_initial"),
        epsilon_floor=lrn.float("epsilon_floor"),
        epsilon_decay=lrn.float("epsilon_decay"),
        controller_seed=lrn.int("controller_seed"),
        queue_edges=lrn.float_list("queue_edges"),
        elapsed_edges_s=lrn.float_list("elapsed_edges_s"),
        tie_abs_tolerance=lrn.float("tie_abs_tolerance"),
        evaluation_requires_both_actions_updated=lrn.bool("evaluation_requires_both_actions_updated"),
    )
    lrn.done()
    _require(0 < learning.alpha <= 1, "learning.alpha: must be in (0, 1]")
    _require(0 < learning.discount_factor <= 1, "learning.discount_factor: must be in (0, 1]")
    _require(learning.discount_unit_s > 0, "learning.discount_unit_s: must be positive")
    _probability(learning.epsilon_initial, "learning.epsilon_initial")
    _probability(learning.epsilon_floor, "learning.epsilon_floor")
    _require(learning.epsilon_floor <= learning.epsilon_initial,
             "learning.epsilon_floor: must not exceed epsilon_initial")
    _require(0 < learning.epsilon_decay <= 1, "learning.epsilon_decay: must be in (0, 1]")
    _require(learning.tie_abs_tolerance > 0, "learning.tie_abs_tolerance: must be positive")
    # Queue bins cover a direction's summed lane scores, each lane in [0, 1].
    q = learning.queue_edges
    _strictly_increasing(q, "learning.queue_edges")
    _require(q[0] == 0.0 and q[-1] == float(LANES_PER_DIRECTION),
             f"learning.queue_edges: must start at 0 and end at {LANES_PER_DIRECTION} (two-lane score range)")
    # Elapsed bins: [0, min green) is not choosable, the last edge is the service limit.
    e = learning.elapsed_edges_s
    _strictly_increasing(e, "learning.elapsed_edges_s")
    _require(len(e) >= 3 and e[0] == 0.0, "learning.elapsed_edges_s: must start at 0 with at least 3 edges")
    _require(e[1] == float(signal.minimum_green_s),
             "learning.elapsed_edges_s: second edge must equal signal.minimum_green_s")
    _require(e[-1] == float(signal.green_service_limit_s),
             "learning.elapsed_edges_s: last edge must equal signal.green_service_limit_s")

    sto = top.section("storage")
    storage = StorageConfig(
        output_root=_resolve(root, sto.str("output_root")),
        checkpoint_every_updates=sto.int("checkpoint_every_updates"),
        keep_previous=sto.int("keep_previous"),
        flush_each_tick=sto.bool("flush_each_tick"),
    )
    sto.done()
    _require(storage.checkpoint_every_updates > 0, "storage.checkpoint_every_updates: must be positive")
    _require(storage.keep_previous == 1, "storage.keep_previous: only latest + one previous is specified")
    _require(storage.flush_each_tick, "storage.flush_each_tick: per-tick flushing is required (Section F)")

    exp = top.section("experiment")
    experiment = ExperimentConfig(
        training_episodes=exp.int("training_episodes"),
        training_seed_start=exp.int("training_seed_start"),
        development_seeds=exp.int_list("development_seeds"),
        final_seeds=exp.int_list("final_seeds"),
        development_after_episodes=exp.int_list("development_after_episodes"),
    )
    exp.done()
    _require(experiment.training_episodes > 0, "experiment.training_episodes: must be positive")
    _require(experiment.training_seed_start >= 0, "experiment.training_seed_start: must be nonnegative")
    for key in ("development_seeds", "final_seeds"):
        seeds = getattr(experiment, key)
        _require(all(s >= 0 for s in seeds) and len(set(seeds)) == len(seeds),
                 f"experiment.{key}: must be unique nonnegative integers")
    _require(not set(experiment.development_seeds) & set(experiment.final_seeds),
             "experiment: development and final seeds must not overlap (never tune on final seeds)")
    _require(all(n > 0 for n in experiment.development_after_episodes),
             "experiment.development_after_episodes: must be positive")
    _strictly_increasing(experiment.development_after_episodes, "experiment.development_after_episodes")

    rob = top.section("robustness")
    levels_section = rob.section("levels")
    levels = {}
    for name in list(levels_section._data):
        level = levels_section.section(name)
        fault = FaultLevel(
            noise_bound_vehicles=level.int("noise_bound_vehicles"),
            stale_probability=level.float("stale_probability"),
        )
        level.done()
        _require(fault.noise_bound_vehicles >= 0, f"robustness.levels.{name}: noise bound must be nonnegative")
        _probability(fault.stale_probability, f"robustness.levels.{name}.stale_probability")
        levels[str(name)] = fault
    levels_section.done()
    _require(bool(levels), "robustness.levels: at least one level is required")
    robustness = RobustnessConfig(
        levels=levels,
        noise_seed_offset=rob.int("noise_seed_offset"),
        saturation_check_max_fraction=rob.float("saturation_check_max_fraction"),
    )
    rob.done()
    _require(robustness.noise_seed_offset >= 0, "robustness.noise_seed_offset: must be nonnegative")
    _probability(robustness.saturation_check_max_fraction, "robustness.saturation_check_max_fraction")

    top.done()
    return Config(
        schema_version=schema_version,
        simulation=simulation,
        network=network,
        signal=signal,
        sensor=sensor,
        learning=learning,
        storage=storage,
        experiment=experiment,
        robustness=robustness,
        source_path=source,
        project_root=root,
    )


def _direction(value: str, where: str) -> Direction:
    try:
        return Direction(value)
    except ValueError as exc:
        raise ConfigError(f"{where}: expected one of {[d.value for d in Direction]}, got {value!r}") from exc


def _lane_list(value: Any, where: str) -> tuple[str, ...]:
    _require(isinstance(value, list) and len(value) == LANES_PER_DIRECTION,
             f"{where}: expected exactly {LANES_PER_DIRECTION} lane IDs, got {value!r}")
    _require(all(isinstance(v, str) and v for v in value), f"{where}: lane IDs must be non-empty strings")
    return tuple(value)


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()
