"""Section F (event schema): envelope, strict JSON, ordering, no overwrite."""

import json

import pytest

from traffic_cps.contracts import Direction, SignalColor
from traffic_cps.events import EventError, EventWriter, create_run_dir, to_jsonable


def test_contract_F_envelope_and_monotonic_sequence(tmp_path):
    w = EventWriter(tmp_path / "events.jsonl", run_id="run-1")
    a = w.append("observation", 0, {"x": 1})
    b = w.append("decision", 0, {"observation_id": a})
    w.close()
    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    for line in lines:
        assert set(line) == {"schema_version", "run_id", "event_id", "seq", "sim_time_s", "type", "payload"}
        assert line["schema_version"] == 1 and line["run_id"] == "run-1"
    assert lines[0]["event_id"] == a and lines[1]["event_id"] == b and a != b
    assert lines[1]["payload"]["observation_id"] == a


def test_contract_F_rejects_nan_and_unknown_types(tmp_path):
    w = EventWriter(tmp_path / "events.jsonl", run_id="run-1")
    with pytest.raises(EventError):
        w.append("observation", 1, {"score": float("nan")})
    with pytest.raises(EventError):
        w.append("made_up_type", 1, {})
    w.close()
    assert (tmp_path / "events.jsonl").read_text() == ""  # nothing partial was written


def test_contract_F_explicit_null_for_unavailable_fields(tmp_path):
    w = EventWriter(tmp_path / "events.jsonl", run_id="run-1")
    w.append("decision", 5, {"epsilon": None})
    w.close()
    assert '"epsilon":null' in (tmp_path / "events.jsonl").read_text()


def test_contract_F_never_overwrites_event_log(tmp_path):
    (tmp_path / "events.jsonl").write_text("existing\n")
    with pytest.raises(EventError):
        EventWriter(tmp_path / "events.jsonl", run_id="run-1")
    assert (tmp_path / "events.jsonl").read_text() == "existing\n"


def test_contract_I_never_overwrites_run_directory(tmp_path):
    first = create_run_dir(tmp_path, "run-1")
    assert first.is_dir()
    with pytest.raises(EventError):
        create_run_dir(tmp_path, "run-1")


def test_contract_F_serializes_records_enums_and_direction_keys():
    data = to_jsonable({"colors": {Direction.NS: SignalColor.GREEN}, "t": (1, 2)})
    assert data == {"colors": {"NS": "green"}, "t": [1, 2]}
