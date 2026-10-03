"""Chunk 5 with real SUMO: checkpoint schedule, resume, interruption, write-failure policy, milestones.

Module 6 acceptance checks, Section F, Section I, and the Section K resolutions of 2026-10-03.
"""

import json
from dataclasses import replace
from functools import partial

import pytest

from traffic_cps import checkpoints as ck
from traffic_cps import events as ev
from traffic_cps.cli import main
from traffic_cps.contracts import CompletionStatus
from traffic_cps.faults import FaultyConnection
from traffic_cps.runtime import StopRequest, train
from tests.integration.harness import CONFIG, PROJECT_ROOT, make_scenario

H = 120
CFG_PATH = str(PROJECT_ROOT / "configs/default.yaml")


def cfg(every=1000, milestones=(), dev_seeds=(100,)):
    return replace(CONFIG,
                   storage=replace(CONFIG.storage, checkpoint_every_updates=every),
                   experiment=replace(CONFIG.experiment, development_after_episodes=tuple(milestones),
                                      development_seeds=tuple(dev_seeds)))


def events_of(run_dir):
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def by_type(events, kind):
    return [e for e in events if e["type"] == kind]


def latest(training_dir):
    return json.loads((training_dir / "checkpoints" / ck.LATEST).read_text())


class SaveCounter:
    """Wrap CheckpointStore.save: count calls and fail the calls whose 1-based numbers are given."""

    def __init__(self, monkeypatch, fail_on=()):
        self.calls, self.fail_on = 0, set(fail_on)
        real = ck.CheckpointStore.save

        def save(store, data):
            self.calls += 1
            if self.calls in self.fail_on:
                raise ck.CheckpointWriteError(f"injected checkpoint failure on save {self.calls}")
            return real(store, data)

        monkeypatch.setattr(ck.CheckpointStore, "save", save)


# ----------------------------------------------------------------------
# Schedule and contents
# ----------------------------------------------------------------------

def test_module6_initial_checkpoint_precedes_traffic(tmp_path, monkeypatch):
    SaveCounter(monkeypatch, fail_on={1})
    outcome = train(cfg(), 1, tmp_path / "t", horizon_s=H)
    assert outcome.persistence_failure.startswith("initial checkpoint failed")
    assert not (tmp_path / "t" / "runs").exists() and not outcome.episodes     # no traffic was started


def test_module6_every_nth_completed_update_triggers_a_checkpoint(tmp_path):
    outcome = train(cfg(every=10), 1, tmp_path / "t", horizon_s=H)
    run = outcome.episodes[0]
    assert run.completion is CompletionStatus.COMPLETED and run.checkpoint_status == "saved"
    saves = by_type(events_of(tmp_path / "t" / "runs" / run.run_id), "checkpoint_saved")
    periodic = [s["payload"] for s in saves if s["payload"]["reason"] == "every_n_updates"]
    assert periodic and [p["progress"]["completed_updates"] for p in periodic] == \
        [10 * (i + 1) for i in range(len(periodic))]                        # exactly the 10th, 20th, ...
    assert saves[-1]["payload"]["reason"] == "episode_end"
    assert [s["payload"]["save_generation"] for s in saves] == list(range(2, len(saves) + 2))  # 1 = initial
    assert run.counters["learning_updates"] > periodic[-1]["progress"]["completed_updates"]   # tail saved at end


def test_module6_episode_end_checkpoint_matches_memory_and_counts_episode(tmp_path):
    outcome = train(cfg(), 1, tmp_path / "t", horizon_s=H)
    data = latest(tmp_path / "t")
    restored = ck.learner_from_checkpoint(CONFIG, data)
    assert restored.table.to_json() == outcome.learner.table.to_json()
    assert restored.schedule.to_json() == outcome.learner.schedule.to_json()
    assert restored.rng.getstate() == outcome.learner.rng.getstate()
    assert data["progress"]["completed_episodes"] == 1 and data["progress"]["episode_attempts"] == 1
    assert data["source_run"]["reason"] == "episode_end"


# ----------------------------------------------------------------------
# Resume
# ----------------------------------------------------------------------

def test_module6_resume_restores_learning_and_matches_uninterrupted_training(tmp_path):
    straight = train(cfg(), 2, tmp_path / "straight", horizon_s=H)
    first = train(cfg(), 1, tmp_path / "split", horizon_s=H)
    resumed = train(cfg(), horizon_s=H, resume=tmp_path / "split" / "checkpoints" / ck.LATEST,
                    additional_episodes=1)
    assert straight.ok and first.ok and resumed.ok
    assert resumed.learner.table.to_json() == straight.learner.table.to_json()
    assert resumed.learner.schedule.to_json() == straight.learner.schedule.to_json()
    assert resumed.learner.rng.getstate() == straight.learner.rng.getstate()     # same exploration draws

    run = resumed.episodes[0]
    assert run.run_id == "train-attempt002-seed001"                               # next attempt, next seed
    manifest = json.loads((tmp_path / "split" / "runs" / run.run_id / "run.manifest.json").read_text())
    source = manifest["provenance"]["learning_source"]
    assert source["kind"] == "resumed" and source["checkpoint_id"] == "split-g00002"
    assert manifest["provenance"]["traffic_seed"] == 1


def test_K_resume_with_different_horizon_is_rejected_before_traffic(tmp_path):
    train(cfg(), 1, tmp_path / "t", horizon_s=H)
    runs_before = sorted((tmp_path / "t" / "runs").iterdir())
    code = main(["train", "--config", CFG_PATH, "--resume", str(tmp_path / "t" / "checkpoints" / ck.LATEST),
                 "--additional-episodes", "1", "--horizon", "300"])
    assert code == 2 and sorted((tmp_path / "t" / "runs").iterdir()) == runs_before


def test_module6_corrupt_latest_stops_resume_and_names_previous_without_loading_it(tmp_path, capsys):
    train(cfg(), 1, tmp_path / "t", horizon_s=H)
    path = tmp_path / "t" / "checkpoints" / ck.LATEST
    path.write_text(path.read_text().replace('"completed_episodes": 1', '"completed_episodes": 0', 1))
    code = main(["train", "--config", CFG_PATH, "--resume", str(path), "--additional-episodes", "1",
                 "--horizon", str(H)])
    err = capsys.readouterr().err
    assert code == 2 and "integrity" in err and ck.PREVIOUS in err and "explicitly" in err
    assert len(list((tmp_path / "t" / "runs").iterdir())) == 1                  # nothing started
    previous = tmp_path / "t" / "checkpoints" / ck.PREVIOUS
    assert main(["train", "--config", CFG_PATH, "--resume", str(previous), "--additional-episodes", "1",
                 "--horizon", str(H)]) == 0                                     # explicit selection works


def test_module6_missing_resume_checkpoint_is_rejected(tmp_path):
    (tmp_path / "t" / "checkpoints").mkdir(parents=True)
    assert main(["train", "--config", CFG_PATH, "--resume", str(tmp_path / "t" / "checkpoints" / ck.LATEST),
                 "--additional-episodes", "1", "--horizon", str(H)]) == 2


# ----------------------------------------------------------------------
# Interruption (Section K) and the write-failure policy
# ----------------------------------------------------------------------

def test_K_interrupt_stops_at_tick_boundary_saves_completed_learning_and_resumes(tmp_path):
    stop = StopRequest()

    def wrap(conn):
        f = FaultyConnection(conn)
        f.at_step(52, partial(stop.request, "interrupt requested (test)"))
        return f

    outcome = train(cfg(), 2, tmp_path / "t", horizon_s=H, stop=stop, connection_wrapper=wrap)
    assert outcome.stopped == "interrupt requested (test)" and not outcome.ok
    (run,) = outcome.episodes
    assert run.completion is CompletionStatus.INCOMPLETE and run.final_valid_time_s == 52
    assert run.checkpoint_status == "saved"
    events = events_of(tmp_path / "t" / "runs" / run.run_id)
    (disc,) = by_type(events, "feedback_discarded")
    assert disc["payload"]["reason"] == "interrupted"
    assert by_type(events, "checkpoint_saved")[-1]["payload"]["reason"] == "shutdown"
    data = latest(tmp_path / "t")
    assert data["progress"]["completed_episodes"] == 0 and data["progress"]["episode_attempts"] == 1
    assert data["progress"]["completed_updates"] == run.counters["learning_updates"] > 0

    resumed = train(cfg(), horizon_s=H, resume=tmp_path / "t" / "checkpoints" / ck.LATEST, additional_episodes=1)
    assert resumed.ok and resumed.episodes[0].run_id == "train-attempt002-seed001"   # failed attempt keeps its ID
    assert resumed.completed_episodes == 1


def test_module6_log_write_failure_stops_reports_outside_log_and_saves_shutdown_checkpoint(tmp_path, monkeypatch):
    notes = []
    real_append = ev.EventWriter.append

    class BrokenFile:
        def write(self, text):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

        closed = False

        def close(self):
            self.closed = True

    def append(self, event_type, sim_time_s, payload, event_id=None):
        if sim_time_s >= 40 and not isinstance(self._fh, BrokenFile):
            self._fh.close()
            self._fh = BrokenFile()
        return real_append(self, event_type, sim_time_s, payload, event_id)

    monkeypatch.setattr(ev.EventWriter, "append", append)
    outcome = train(cfg(), 1, tmp_path / "t", horizon_s=H, notify=notes.append)
    (run,) = outcome.episodes
    assert run.completion is CompletionStatus.INCOMPLETE and not run.persistence_ok and not run.trace_complete
    assert run.checkpoint_status == "saved" and not outcome.ok
    assert any("shutdown checkpoint saved" in n for n in notes)                 # reported outside the log
    assert any("trace is incomplete" in n for n in notes)
    monkeypatch.undo()
    events = events_of(tmp_path / "t" / "runs" / run.run_id)
    assert max(e["sim_time_s"] for e in events) < 40                             # nothing written after failure
    assert latest(tmp_path / "t")["source_run"]["reason"] == "shutdown"


def test_module6_checkpoint_write_failure_keeps_previous_stops_and_does_not_retry(tmp_path, monkeypatch):
    counter = SaveCounter(monkeypatch, fail_on={2})                              # 1 = initial, 2 = first periodic
    notes = []
    outcome = train(cfg(every=5), 1, tmp_path / "t", horizon_s=H, notify=notes.append)
    (run,) = outcome.episodes
    assert run.completion is CompletionStatus.INCOMPLETE and run.checkpoint_status == "failed"
    assert counter.calls == 2                                                    # no shutdown retry
    assert latest(tmp_path / "t")["progress"]["save_generation"] == 1           # previous valid file kept
    events = events_of(tmp_path / "t" / "runs" / run.run_id)
    (err,) = [e for e in by_type(events, "error") if e["payload"]["stage"] == "checkpoint_write"]
    assert err["payload"]["last_saved_checkpoint"] == "t-g00001"
    assert events[-1]["type"] == "run_end" and events[-1]["payload"]["checkpoint_status"] == "failed"
    assert any("not attempted" in n for n in notes) and outcome.persistence_failure


def test_module6_final_save_failure_reports_completed_simulation_and_failed_saving(tmp_path, monkeypatch, capsys):
    SaveCounter(monkeypatch, fail_on={2})                                        # 2 = episode_end
    code = main(["train", "--config", CFG_PATH, "--episodes", "1", "--horizon", str(H),
                 "--output", str(tmp_path / "t")])
    assert code == 1
    (run_dir,) = list((tmp_path / "t" / "runs").iterdir())
    end = events_of(run_dir)[-1]["payload"]
    assert end["completion"] == "completed" and end["checkpoint_status"] == "failed"
    assert end["failure"].startswith("simulation completed, saving failed")


def test_module6_both_outputs_failing_terminates_without_looping(tmp_path, monkeypatch):
    counter = SaveCounter(monkeypatch, fail_on={2, 3, 4})
    real_append = ev.EventWriter.append

    def append(self, event_type, sim_time_s, payload, event_id=None):
        if sim_time_s >= 30 and self.failed is None:
            self.failed = "injected log failure"
        return real_append(self, event_type, sim_time_s, payload, event_id)

    monkeypatch.setattr(ev.EventWriter, "append", append)
    notes = []
    outcome = train(cfg(), 1, tmp_path / "t", horizon_s=H, notify=notes.append)
    (run,) = outcome.episodes
    assert run.completion is CompletionStatus.INCOMPLETE and not run.persistence_ok
    assert run.checkpoint_status == "failed" and counter.calls == 2              # one shutdown attempt only
    assert any("shutdown checkpoint failed" in n and "t-g00001" in n for n in notes)


# ----------------------------------------------------------------------
# Frozen evaluation of a saved checkpoint, milestones, development evaluation
# ----------------------------------------------------------------------

def test_contract_I_run_q_learning_from_checkpoint_is_frozen_and_records_provenance(network_dir, tmp_path):
    outcome = train(cfg(), 1, tmp_path / "t", horizon_s=H)
    checkpoint = tmp_path / "t" / "checkpoints" / ck.LATEST
    before = checkpoint.read_bytes()
    scenario = make_scenario(network_dir, tmp_path / "eval_scn", seed=100, horizon=60)
    out = tmp_path / "eval"
    assert main(["run", "--config", CFG_PATH, "--controller", "q_learning", "--checkpoint", str(checkpoint),
                 "--scenario", str(scenario), "--horizon", "60", "--output", str(out)]) == 0
    (run_dir,) = list((out / "runs").iterdir())
    manifest = json.loads((run_dir / "run.manifest.json").read_text())
    assert outcome.ok
    assert manifest["provenance"]["checkpoint_id"] == latest(tmp_path / "t")["checkpoint_id"]
    assert manifest["horizon_s"] == 60                                           # K: evaluation horizon may differ
    events = events_of(run_dir)
    assert by_type(events, "decision") and not by_type(events, "learning_update")
    assert checkpoint.read_bytes() == before                                     # evaluation never writes it


def test_contract_I_run_q_learning_rejects_bad_checkpoint_or_missing_flag(network_dir, tmp_path):
    scenario = make_scenario(network_dir, tmp_path / "scn", horizon=60)
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    base = ["run", "--config", CFG_PATH, "--scenario", str(scenario), "--horizon", "60",
            "--output", str(tmp_path / "o")]
    assert main(base + ["--controller", "q_learning", "--checkpoint", str(bad)]) == 2
    assert main(base + ["--controller", "q_learning"]) == 2
    assert main(base + ["--controller", "queue", "--checkpoint", str(bad)]) == 2
    assert not (tmp_path / "o").exists()


def test_sectionF_milestones_are_immutable_and_evaluated_without_touching_training(tmp_path):
    plain = train(cfg(), 2, tmp_path / "plain", horizon_s=H)
    with_dev = train(cfg(milestones=(1, 2)), 2, tmp_path / "m", horizon_s=H)
    assert with_dev.ok
    assert with_dev.learner.table.to_json() == plain.learner.table.to_json()    # no training RNG consumed
    assert with_dev.learner.rng.getstate() == plain.learner.rng.getstate()
    for n in (1, 2):
        milestone = tmp_path / "m" / "milestones" / f"episode_{n:03d}.json"
        data = json.loads(milestone.read_text())
        assert data["progress"]["completed_episodes"] == n
        run_dir = tmp_path / "m" / "development" / f"episode_{n:03d}" / "runs" / f"dev-ep{n:03d}-seed100"
        manifest = json.loads((run_dir / "run.manifest.json").read_text())
        assert manifest["provenance"]["checkpoint_file_sha256"] == ck.file_sha256(milestone)
        assert manifest["mode"] == "evaluate"
        events = events_of(run_dir)
        assert by_type(events, "decision") and not by_type(events, "learning_update")
    assert [(d["episode"], d["seed"], d["ok"]) for d in with_dev.development] == [(1, 100, True), (2, 100, True)]


def test_K_failed_development_evaluation_is_reported_and_training_continues(tmp_path, monkeypatch):
    def broken(config, data):
        raise RuntimeError("injected development evaluation failure")

    monkeypatch.setattr(ck, "learner_from_checkpoint", broken)
    notes = []
    outcome = train(cfg(milestones=(1,)), 2, tmp_path / "m", horizon_s=H, notify=notes.append)
    (record,) = outcome.development
    assert record["episode"] == 1 and not record["ok"] and "injected" in record["failure"]
    assert outcome.completed_episodes == 2 and len(outcome.episodes) == 2      # training went on
    assert not outcome.ok                                                        # so `train` exits 1
    assert any("development evaluation episode 1 seed 100 failed" in n for n in notes)


def test_module6_resume_from_previous_never_reuses_attempt_ids_or_seeds(tmp_path):
    """Explicitly resuming an older checkpoint rolls back learning, not the attempt index on disk."""
    train(cfg(), 1, tmp_path / "t", horizon_s=H)
    previous = tmp_path / "t" / "checkpoints" / ck.PREVIOUS                    # the initial save: 0 attempts
    assert json.loads(previous.read_text())["progress"]["episode_attempts"] == 0
    resumed = train(cfg(), horizon_s=H, resume=previous, additional_episodes=1)
    assert resumed.ok and resumed.episodes[0].run_id == "train-attempt002-seed001"
    assert sorted(p.name for p in (tmp_path / "t" / "runs").iterdir()) == [
        "train-attempt001-seed000", "train-attempt002-seed001"]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "no_index"])
def test_contract_B_resume_stops_on_missing_or_corrupt_run_manifest(tmp_path, capsys, damage):
    train(cfg(), 1, tmp_path / "t", horizon_s=H)
    manifest = tmp_path / "t" / "runs" / "train-attempt001-seed000" / "run.manifest.json"
    if damage == "missing":
        manifest.unlink()
    elif damage == "corrupt":
        manifest.write_text("{not json")
    else:
        data = json.loads(manifest.read_text())
        del data["provenance"]["episode_attempt_index"]
        manifest.write_text(json.dumps(data))
    code = main(["train", "--config", CFG_PATH, "--resume", str(tmp_path / "t" / "checkpoints" / ck.PREVIOUS),
                 "--additional-episodes", "1", "--horizon", str(H)])
    assert code == 2 and "recovery error" in capsys.readouterr().err
    assert len(list((tmp_path / "t" / "runs").iterdir())) == 1                  # nothing started


def test_contract_B_next_attempt_follows_highest_manifest_index_not_folder_count(tmp_path):
    train(cfg(), 1, tmp_path / "t", horizon_s=H)
    manifest = tmp_path / "t" / "runs" / "train-attempt001-seed000" / "run.manifest.json"
    data = json.loads(manifest.read_text())
    data["provenance"]["episode_attempt_index"] = 3                              # as if attempts 2-4 were allocated
    manifest.write_text(json.dumps(data))
    resumed = train(cfg(), horizon_s=H, resume=tmp_path / "t" / "checkpoints" / ck.LATEST, additional_episodes=1)
    assert resumed.ok and resumed.episodes[0].run_id == "train-attempt005-seed004"
