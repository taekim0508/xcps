"""Build contract Section I: CLI exit codes for chunk-0 commands (no SUMO)."""

import stat

from traffic_cps.cli import main


def _fake(tmp_path, name, version="1.24.0"):
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\necho 'Eclipse SUMO {name} Version {version}'\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _doctor_args(tmp_path, config, version="1.24.0"):
    return [
        "doctor", "--config", str(config),
        "--sumo", _fake(tmp_path, "sumo", version),
        "--netconvert", _fake(tmp_path, "netconvert", version),
    ]


def test_contract_I_doctor_valid_config_and_toolchain_exits_0(tmp_path, default_config_dict, write_config):
    assert main(_doctor_args(tmp_path, write_config(default_config_dict))) == 0


def test_contract_I_doctor_invalid_config_exits_2(tmp_path, default_config_dict, write_config):
    default_config_dict["signal"]["all_red_s"] = 2
    assert main(_doctor_args(tmp_path, write_config(default_config_dict))) == 2


def test_contract_I_doctor_wrong_simulator_version_exits_2(tmp_path, default_config_dict, write_config):
    assert main(_doctor_args(tmp_path, write_config(default_config_dict), version="1.20.0")) == 2


def test_contract_I_demand_refuses_existing_output_exit_2(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("do not overwrite")
    net = tmp_path / "net"
    net.mkdir()
    code = main([
        "scenario", "demand", "--network", str(net), "--seed", "0",
        "--profile", "balanced", "--horizon", "60", "--output", str(out),
    ])
    assert code == 2
    assert (out / "keep.txt").read_text() == "do not overwrite"
