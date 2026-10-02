"""Build contract Section A: `doctor` toolchain checks, using fake executables."""

import os
import stat

import pytest

from traffic_cps.cli import DoctorError, check_binary, parse_sumo_version


def _fake_binary(tmp_path, name, stdout="", stderr="", code=0):
    path = tmp_path / name
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s' '{stdout}'\n"
        f"printf '%s' '{stderr}' >&2\n"
        f"exit {code}\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Eclipse SUMO sumo Version 1.24.0\n Build features: ...", "1.24.0"),
        ("Eclipse SUMO netconvert Version 1.24.0\n", "1.24.0"),
        ("Eclipse SUMO sumo Version v1_24_0+0123-abc\n", None),
        ("garbage", None),
    ],
)
def test_contract_A_parse_sumo_version(text, expected):
    assert parse_sumo_version(text) == expected


def test_contract_A_doctor_accepts_pinned_version(tmp_path):
    exe = _fake_binary(tmp_path, "sumo", stdout="Eclipse SUMO sumo Version 1.24.0")
    assert check_binary("sumo", explicit_path=str(exe), expected="1.24.0") == ("1.24.0", str(exe))


def test_contract_A_doctor_rejects_other_version(tmp_path):
    exe = _fake_binary(tmp_path, "sumo", stdout="Eclipse SUMO sumo Version 1.25.0")
    with pytest.raises(DoctorError, match="1.25.0"):
        check_binary("sumo", explicit_path=str(exe), expected="1.24.0")


def test_contract_A_doctor_reports_missing_native_library(tmp_path):
    exe = _fake_binary(
        tmp_path, "sumo",
        stderr="error while loading shared libraries: libX11.so.6: cannot open shared object file",
        code=127,
    )
    with pytest.raises(DoctorError, match="libX11.so.6"):
        check_binary("sumo", explicit_path=str(exe), expected="1.24.0")


def test_contract_A_doctor_reports_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(DoctorError, match="not found"):
        check_binary("netconvert", explicit_path=None, expected="1.24.0")


def test_contract_A_doctor_explicit_path_wins_over_path(tmp_path, monkeypatch):
    on_path = tmp_path / "onpath"
    on_path.mkdir()
    _fake_binary(on_path, "sumo", stdout="Eclipse SUMO sumo Version 1.0.0")
    monkeypatch.setenv("PATH", str(on_path) + os.pathsep + os.environ["PATH"])
    explicit = _fake_binary(tmp_path, "mysumo", stdout="Eclipse SUMO sumo Version 1.24.0")
    assert check_binary("sumo", explicit_path=str(explicit), expected="1.24.0")[0] == "1.24.0"
    with pytest.raises(DoctorError):
        check_binary("sumo", explicit_path=None, expected="1.24.0")
