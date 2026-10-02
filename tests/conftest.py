from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yaml"


@pytest.fixture
def default_config_dict():
    """A fresh, mutable copy of configs/default.yaml as plain data."""
    return yaml.safe_load(DEFAULT_CONFIG.read_text())


@pytest.fixture
def write_config(tmp_path):
    """Write a config dict into a throwaway project root and return its path."""

    def _write(data):
        (tmp_path / "pyproject.toml").write_text("")
        config_dir = tmp_path / "configs"
        config_dir.mkdir(exist_ok=True)
        path = config_dir / "test.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path

    return _write
