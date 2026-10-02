"""Build contract Section C: default configuration and validation."""

import copy
import re

import pytest

from traffic_cps.config import ConfigError, config_digest, load_config
from tests.conftest import DEFAULT_CONFIG, PROJECT_ROOT


def test_contract_C_default_config_loads_and_resolves_paths():
    cfg = load_config(DEFAULT_CONFIG)
    assert cfg.simulation.horizon_s == 3600
    assert cfg.network.lanes == {"NS": ("N_J_0", "N_J_1"), "WE": ("W_J_0", "W_J_1")}
    assert cfg.learning.tie_abs_tolerance == pytest.approx(1e-9, abs=0)
    assert cfg.network.directory == PROJECT_ROOT / "scenarios" / "single"
    assert cfg.storage.output_root == PROJECT_ROOT / "outputs"


def test_contract_C_digest_is_stable_and_sensitive():
    a = load_config(DEFAULT_CONFIG)
    b = load_config(DEFAULT_CONFIG)
    assert config_digest(a) == config_digest(b)


def test_contract_C_digest_changes_with_values(default_config_dict, write_config):
    base = load_config(write_config(default_config_dict))
    default_config_dict["learning"]["alpha"] = 0.2
    changed = load_config(write_config(default_config_dict))
    assert config_digest(base) != config_digest(changed)


def _set(data, dotted, value):
    keys = dotted.split(".")
    node = data
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = value


def _delete(data, dotted):
    keys = dotted.split(".")
    node = data
    for key in keys[:-1]:
        node = node[key]
    del node[keys[-1]]


# Each case is one rejection rule from Section C, plus type strictness.
REJECTIONS = [
    ("unknown top-level key", "extra_section", {"a": 1}, "unknown key"),
    ("unknown nested key", "signal.red_s", 3, "unknown key"),
    ("nonfinite float", "learning.alpha", float("nan"), "finite"),
    ("infinite float", "learning.discount_factor", float("inf"), "finite"),
    ("probability above 1", "learning.epsilon_initial", 1.5, "probability"),
    ("negative probability", "learning.epsilon_floor", -0.1, "probability"),
    ("floor above initial epsilon", "learning.epsilon_initial", 0.05, "must not exceed epsilon_initial"),
    ("zero alpha", "learning.alpha", 0.0, "alpha"),
    ("duplicate lane ids", "network.lanes.WE", ["N_J_0", "W_J_1"], "duplicate lane"),
    ("missing direction mapping", "network.lanes", {"NS": ["N_J_0", "N_J_1"]}, "lanes.WE: required"),
    ("extra direction mapping", "network.lanes", {"NS": ["a", "b"], "WE": ["c", "d"], "EW": ["e", "f"]}, "unknown key"),
    ("one lane per direction", "network.lanes.NS", ["N_J_0"], "exactly 2"),
    ("step size other than 1", "simulation.step_s", 2, "1-second"),
    ("nonpositive yellow", "signal.yellow_s", 0, "yellow_s: must be positive"),
    ("nonpositive minimum green", "signal.minimum_green_s", -5, "minimum_green_s: must be positive"),
    ("minimum green equal to limit", "signal.minimum_green_s", 60, "below green_service_limit"),
    ("nonzero all red", "signal.all_red_s", 1, "all-red"),
    ("fixed target equal to limit", "signal.fixed_green_target_s", 60, "strictly below"),
    ("fixed target above limit", "signal.fixed_green_target_s", 75, "strictly below"),
    ("candidate at limit", "signal.fixed_green_candidates_s", [15, 60], "candidates_s: 60"),
    ("nonpositive candidate", "signal.fixed_green_candidates_s", [0, 15], "candidates_s: 0"),
    ("negative noise bound", "robustness.levels.low.noise_bound_vehicles", -1, "noise bound"),
    ("stale probability above 1", "robustness.levels.high.stale_probability", 1.2, "probability"),
    ("stale probability negative", "robustness.levels.low.stale_probability", -0.01, "probability"),
    ("horizon not divisible by 5", "simulation.horizon_s", 3601, "divisible by 5"),
    ("zero horizon", "simulation.horizon_s", 0, "positive integer"),
    ("float horizon", "simulation.horizon_s", 3600.0, "expected an integer"),
    ("bool as integer", "simulation.horizon_s", True, "expected an integer"),
    ("integer as bool", "simulation.gui", 0, "true/false"),
    ("queue edges not starting at 0", "learning.queue_edges", [0.1, 0.4, 0.8, 1.2, 1.6, 2.0], "start at 0 and end at 2"),
    ("queue edges not ending at lane count", "learning.queue_edges", [0.0, 0.2, 0.4, 0.6, 0.8, 1.0], "start at 0 and end at 2"),
    ("queue edges not increasing", "learning.queue_edges", [0.0, 0.8, 0.4, 1.2, 1.6, 2.0], "strictly increasing"),
    ("elapsed edges not starting at 0", "learning.elapsed_edges_s", [1.0, 5.0, 32.5, 60.0], "must start at 0"),
    ("elapsed edges not aligned to minimum green", "learning.elapsed_edges_s", [0.0, 4.0, 32.5, 60.0], "minimum_green_s"),
    ("elapsed edges not ending at limit", "learning.elapsed_edges_s", [0.0, 5.0, 32.5, 55.0], "green_service_limit_s"),
    ("nonpositive tie tolerance", "learning.tie_abs_tolerance", 0.0, "tie_abs_tolerance"),
    ("initial green not a direction", "simulation.initial_green", "EW", "expected one of"),
    ("unsupported schema version", "schema_version", 2, "unsupported"),
    ("unsupported sumo version", "simulation.sumo_version", "1.25.0", "pinned toolchain"),
    ("saturation fraction above 1", "robustness.saturation_check_max_fraction", 1.5, "probability"),
    ("dev and final seeds overlap", "experiment.final_seeds", [102, 200], "must not overlap"),
]


@pytest.mark.parametrize("label,path,value,reason", REJECTIONS, ids=[r[0] for r in REJECTIONS])
def test_contract_C_rejects_invalid_config(default_config_dict, write_config, label, path, value, reason):
    data = copy.deepcopy(default_config_dict)
    _set(data, path, value)
    # Matching the reason guards against a case passing because a different rule fired first.
    with pytest.raises(ConfigError, match=re.escape(reason)):
        load_config(write_config(data))


@pytest.mark.parametrize("path", ["simulation.tls_id", "learning", "storage.keep_previous"])
def test_contract_C_rejects_missing_keys(default_config_dict, write_config, path):
    _delete(default_config_dict, path)
    with pytest.raises(ConfigError):
        load_config(write_config(default_config_dict))


def test_contract_C_rejects_duplicate_yaml_keys(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    text = DEFAULT_CONFIG.read_text().replace("  yellow_s: 2\n", "  yellow_s: 2\n  yellow_s: 3\n")
    path = tmp_path / "dup.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(path)


def test_contract_C_rejects_unsafe_yaml_tags(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    path = tmp_path / "evil.yaml"
    path.write_text("schema_version: !!python/object/apply:os.system ['true']\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_contract_C_missing_project_root_is_explicit(tmp_path, default_config_dict):
    import yaml

    path = tmp_path / "orphan.yaml"
    path.write_text(yaml.safe_dump(default_config_dict))
    with pytest.raises(ConfigError, match="project root"):
        load_config(path)
