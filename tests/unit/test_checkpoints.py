"""Module 6 / Section F checkpoints without SUMO: contents, integrity, compatibility, safe rotation, restore."""

import json
import os
import random

import pytest

from traffic_cps import checkpoints as ck
from traffic_cps.config import load_config
from traffic_cps.contracts import Direction, QState
from traffic_cps.learning import Learner

NS, WE = Direction.NS, Direction.WE
CONFIG = load_config("configs/default.yaml")
NETWORK = {"content_sha256": "a" * 64,
           "incoming_lane_lengths_m": {"N_J_0": 246.0, "N_J_1": 246.0, "W_J_0": 239.6, "W_J_1": 239.6}}


def compat(horizon=3600, config=CONFIG):
    return ck.compatibility(config, NETWORK, horizon)


def trained_learner():
    lr = Learner(CONFIG)
    lr.table.ensure(QState(0, 0, NS, 1))                  # a zero entry that must survive
    lr.table._set(QState(1, 2, WE, 2), NS, -3.25, 4)
    lr.table._set(QState(1, 2, WE, 2), WE, 0.0, 2)        # a zero value with updates
    lr.table.revision = 6
    lr.completed_updates = 6
    for _ in range(7):
        lr.schedule.accepted()
    for _ in range(11):
        lr.rng.random()
    return lr


def snapshot(lr=None, generation=1, **progress):
    p = ck.Progress(completed_episodes=progress.get("completed", 1), episode_attempts=progress.get("attempts", 2),
                    save_generation=generation)
    return ck.build_checkpoint(lr or trained_learner(), p, "training_001", compat(), {"run_id": "r1", "reason": "test"})


# ----------------------------------------------------------------------
# Contents and integrity
# ----------------------------------------------------------------------

def test_sectionF_checkpoint_has_exactly_the_specified_top_level_keys():
    assert set(snapshot()) == {"schema_version", "checkpoint_id", "training_id", "created_at_utc", "compatibility",
                               "progress", "learning", "controller_rng", "q_table", "source_run",
                               "integrity_sha256"}


def test_module6_round_trip_preserves_zero_entries_values_counts_epsilon_rng_and_progress():
    lr = trained_learner()
    data = json.loads(json.dumps(snapshot(lr)))
    ck.validate_checkpoint(data)
    back = ck.learner_from_checkpoint(CONFIG, data)
    assert back.table.to_json() == lr.table.to_json()                       # zero entry kept, nothing filtered
    assert back.table.contains(QState(0, 0, NS, 1))
    assert back.schedule.to_json() == lr.schedule.to_json()
    assert back.completed_updates == 6 and back.table.revision == 6
    assert [back.rng.random() for _ in range(5)] == [lr.rng.random() for _ in range(5)]
    progress = ck.progress_from_checkpoint(data)
    assert (progress.completed_episodes, progress.episode_attempts, progress.save_generation) == (1, 2, 1)
    assert data["progress"]["accepted_choices"] == 7 and data["progress"]["policy_revision"] == 6


def test_sectionF_integrity_covers_everything_but_itself():
    data = snapshot()
    assert data["integrity_sha256"] == ck.integrity_of(data)
    tampered = json.loads(json.dumps(data))
    tampered["q_table"][0]["values"]["NS"] = -1.0
    with pytest.raises(ck.CheckpointError, match="integrity"):
        ck.validate_checkpoint(tampered)


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d.pop("controller_rng"), "keys"),
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d["learning"].update(epsilon=1.5), "epsilon"),
    (lambda d: d["learning"].update(epsilon=0.05), "epsilon"),
    (lambda d: d["progress"].update(completed_episodes=5), "completed_episodes"),
    (lambda d: d["progress"].update(completed_updates=-1), "completed_updates"),
    (lambda d: d["controller_rng"]["state"][1].pop(), "controller_rng"),
    (lambda d: d["controller_rng"]["state"].__setitem__(0, 2), "controller_rng"),
    (lambda d: d["q_table"][0]["counts"].update(NS=-1), "count"),
    (lambda d: d["q_table"].append(d["q_table"][0]), "duplicate"),
])
def test_sectionF_structural_problems_are_rejected_even_with_a_matching_digest(mutate, match):
    data = json.loads(json.dumps(snapshot()))
    mutate(data)
    data["integrity_sha256"] = ck.integrity_of(data)    # an accidental-corruption digest cannot vouch for content
    with pytest.raises(ck.CheckpointError, match=match):
        ck.validate_checkpoint(data)


def test_sectionF_strict_json_no_nan():
    lr = trained_learner()
    lr.table._set(QState(0, 0, NS, 1), NS, float("nan"), 1)
    with pytest.raises(ck.CheckpointError, match="finite"):
        ck.build_checkpoint(lr, ck.Progress(0, 1, 1), "t", compat(), None)


# ----------------------------------------------------------------------
# Compatibility
# ----------------------------------------------------------------------

def test_sectionF_compatibility_contract_contents():
    c = compat()
    for key in ("network_content_sha256", "queue_formula_id", "lane_lengths_m", "lane_capacities", "queue_edges",
                "elapsed_edges_s", "action_order", "timing", "horizon_s", "tie_abs_tolerance", "alpha",
                "discount", "exploration", "coverage_rule", "versions", "controller_schema_version"):
        assert key in c, key
    assert c["queue_formula_id"] == "halting_capacity_v1" and c["action_order"] == ["NS", "WE"]
    assert c["versions"]["python"] == "3.11" and c["versions"]["sumo"] == "1.24.0"
    assert c["lane_capacities"]["N_J_0"] == pytest.approx(246.0 / 7.5)


def test_sectionF_incompatible_learning_semantics_rejected_for_resume_and_evaluation():
    from dataclasses import replace
    other = replace(CONFIG, learning=replace(CONFIG.learning, alpha=0.2))
    for purpose in ("resume", "evaluate"):
        with pytest.raises(ck.CheckpointError, match="alpha"):
            ck.check_compatibility(compat(), compat(config=other), purpose)


def test_K_horizon_must_match_to_resume_but_not_to_evaluate():
    with pytest.raises(ck.CheckpointError, match="horizon_s"):
        ck.check_compatibility(compat(3600), compat(300), "resume")
    ck.check_compatibility(compat(3600), compat(300), "evaluate")


def test_K_python_patch_version_does_not_block_but_sumo_version_does():
    saved = compat()
    assert saved["versions"]["python"] == "3.11"                   # major.minor only
    changed = json.loads(json.dumps(saved))
    changed["versions"]["traci"] = "1.25.0"
    with pytest.raises(ck.CheckpointError, match="versions"):
        ck.check_compatibility(saved, changed, "resume")


# ----------------------------------------------------------------------
# Store: safe write, rotation, failures, loading
# ----------------------------------------------------------------------

def test_module6_first_save_publishes_latest_without_previous(tmp_path):
    store = ck.CheckpointStore(tmp_path / "checkpoints")
    result = store.save(snapshot(generation=1))
    assert result.status == "saved" and result.path == store.latest
    assert store.latest.is_file() and not store.previous.exists()
    assert not [p for p in store.directory.iterdir() if p.name.startswith(".")]   # no temp files left


def test_module6_rotation_keeps_validated_old_latest_as_previous(tmp_path):
    store = ck.CheckpointStore(tmp_path / "checkpoints")
    store.save(snapshot(generation=1))
    first = store.latest.read_bytes()
    store.save(snapshot(generation=2))
    assert store.previous.read_bytes() == first
    assert json.loads(store.latest.read_text())["progress"]["save_generation"] == 2


def test_module6_corrupt_latest_is_never_rotated_into_previous(tmp_path):
    store = ck.CheckpointStore(tmp_path / "checkpoints")
    store.save(snapshot(generation=1))
    store.save(snapshot(generation=2))
    good_previous = store.previous.read_bytes()
    store.latest.write_text("{broken")
    store.save(snapshot(generation=3))
    assert store.previous.read_bytes() == good_previous


def test_module6_failed_temp_write_leaves_previous_latest_intact(tmp_path, monkeypatch):
    store = ck.CheckpointStore(tmp_path / "checkpoints")
    store.save(snapshot(generation=1))
    before = store.latest.read_bytes()

    def fail(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ck.os, "fsync", fail)
    with pytest.raises(ck.CheckpointWriteError) as info:
        store.save(snapshot(generation=2))
    assert info.value.status == "failed"
    monkeypatch.undo()
    assert store.latest.read_bytes() == before and not store.previous.exists()
    assert not [p for p in store.directory.iterdir() if p.name.startswith(".")]


def test_module6_interrupted_publication_leaves_latest_valid(tmp_path, monkeypatch):
    """A crash between writing the temp file and replacing latest: latest is still the old valid file."""
    store = ck.CheckpointStore(tmp_path / "checkpoints")
    store.save(snapshot(generation=1))
    before = store.latest.read_bytes()
    real_replace = os.replace

    def crash_on_latest(src, dst):
        if str(dst).endswith(ck.LATEST):
            raise KeyboardInterrupt("simulated crash before publication")
        return real_replace(src, dst)

    monkeypatch.setattr(ck.os, "replace", crash_on_latest)
    with pytest.raises(KeyboardInterrupt):
        store.save(snapshot(generation=2))
    monkeypatch.undo()
    assert store.latest.read_bytes() == before
    loaded = store.load(store.latest, compat(), "resume")
    assert loaded["progress"]["save_generation"] == 1
    store.save(snapshot(generation=3))                                 # a later save still works
    assert json.loads(store.latest.read_text())["progress"]["save_generation"] == 3


def test_module6_missing_corrupt_incompatible_loads_are_rejected(tmp_path):
    store = ck.CheckpointStore(tmp_path / "checkpoints")
    with pytest.raises(ck.CheckpointError, match="missing"):
        store.load(store.latest, compat(), "resume")
    store.save(snapshot(generation=1))
    with pytest.raises(ck.CheckpointError, match="horizon_s"):
        store.load(store.latest, compat(300), "resume")
    store.latest.write_text("not json")
    with pytest.raises(ck.CheckpointError, match="corrupt"):
        store.load(store.latest, compat(), "resume")


def test_module6_failed_latest_names_valid_previous_but_never_loads_it(tmp_path):
    store = ck.CheckpointStore(tmp_path / "checkpoints")
    store.save(snapshot(generation=1))
    store.save(snapshot(generation=2))
    store.latest.write_text("{broken")
    with pytest.raises(ck.CheckpointError) as info:
        store.load(store.latest, compat(), "resume")
    assert str(store.previous) in str(info.value) and "explicitly" in str(info.value)
    assert store.load(store.previous, compat(), "resume")["progress"]["save_generation"] == 1


def test_sectionF_milestone_copy_is_immutable(tmp_path):
    store = ck.CheckpointStore(tmp_path / "checkpoints")
    store.save(snapshot(generation=1))
    path = store.write_milestone(tmp_path / "milestones", 5)
    assert path.name == "episode_005.json" and path.read_bytes() == store.latest.read_bytes()
    with pytest.raises(ck.CheckpointWriteError, match="exists"):
        store.write_milestone(tmp_path / "milestones", 5)


def test_sectionF_rng_state_restores_tuples_without_pickle():
    rng = random.Random(0)
    for _ in range(3):
        rng.random()
    data = ck.rng_to_json(rng)
    assert json.loads(json.dumps(data)) == data
    back = ck.rng_from_json(json.loads(json.dumps(data)))
    assert back.getstate() == rng.getstate()
