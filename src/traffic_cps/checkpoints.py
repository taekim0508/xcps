"""Training checkpoints (Module 6, build contract Section F).

A checkpoint is one consistent JSON snapshot of the learner (Q-table with
completed-update counts, exploration progress, controller RNG) plus training
progress and a compatibility contract. The event log is history; this file is
what restores learning into memory.

Publication is safe by construction: write a same-directory temporary file,
flush and fsync it, re-read and validate it, preserve the validated old latest
as previous (its own temp + replace), then atomically replace latest and try a
directory fsync. A failure at any step leaves the last valid latest in place.
The SHA-256 integrity field detects accidental corruption, not tampering.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from traffic_cps.learning import ACTIONS, QUEUE_FORMULA_ID, ExplorationSchedule, Learner, LearningError, QTable

SCHEMA_VERSION = 1
CONTROLLER_SCHEMA_VERSION = 1
LATEST = "training_latest.json"
PREVIOUS = "training_previous.json"
TOP_LEVEL_KEYS = frozenset({"schema_version", "checkpoint_id", "training_id", "created_at_utc", "compatibility",
                            "progress", "learning", "controller_rng", "q_table", "source_run", "integrity_sha256"})
RNG_IMPLEMENTATION = "python random.Random (MT19937)"
# Compatibility keys that frozen evaluation may differ on (design doc Section K: horizon).
EVALUATION_MAY_DIFFER = frozenset({"horizon_s"})


class CheckpointError(RuntimeError):
    """A checkpoint is missing, corrupt, structurally invalid, or incompatible. Nothing is loaded."""


class CheckpointWriteError(RuntimeError):
    """A save did not publish a new latest checkpoint (status "failed") or its publication is uncertain
    ("unconfirmed"). The previous valid latest, if any, is untouched in the "failed" case."""

    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Progress:
    completed_episodes: int
    episode_attempts: int
    save_generation: int


@dataclass(frozen=True)
class SaveResult:
    status: str               # always "saved": failures raise CheckpointWriteError instead
    path: Path
    checkpoint_id: str
    generation: int
    integrity_sha256: str
    file_sha256: str
    durability: str           # whether the directory entry was fsynced too (platform dependent)


# ----------------------------------------------------------------------
# Canonical JSON and integrity
# ----------------------------------------------------------------------

def canonical_json(data: Any) -> str:
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError as exc:
        raise CheckpointError(f"checkpoint contains a value that is not finite JSON: {exc}") from exc


def integrity_of(data: dict) -> str:
    body = {k: v for k, v in data.items() if k != "integrity_sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Compatibility contract
# ----------------------------------------------------------------------

def compatibility(config, network_manifest: dict, horizon_s: int) -> dict:
    """Everything that changes what a learned value means. Paths, seeds, and run names are excluded."""
    lc, sig, sim, sen = config.learning, config.signal, config.simulation, config.sensor
    lengths = {lane: float(network_manifest["incoming_lane_lengths_m"][lane])
               for lanes in config.network.lanes_by_direction().values() for lane in lanes}
    spacing = sen.vehicle_length_m + sen.minimum_gap_m
    return {
        "network_content_sha256": network_manifest["content_sha256"],
        "lanes": {d.value: list(lanes) for d, lanes in config.network.lanes_by_direction().items()},
        "queue_formula_id": QUEUE_FORMULA_ID,
        "lane_lengths_m": lengths,
        "lane_capacities": {lane: length / spacing for lane, length in lengths.items()},
        "queue_edges": list(lc.queue_edges),
        "elapsed_edges_s": list(lc.elapsed_edges_s),
        "action_order": [a.value for a in ACTIONS],
        "timing": {"step_s": sim.step_s, "control_interval_s": sim.control_interval_s, "yellow_s": sig.yellow_s,
                   "minimum_green_s": sig.minimum_green_s, "green_service_limit_s": sig.green_service_limit_s,
                   "all_red_s": sig.all_red_s, "initial_green": sim.initial_green.value},
        "horizon_s": horizon_s,
        "tie_abs_tolerance": lc.tie_abs_tolerance,
        "alpha": lc.alpha,
        "discount": {"factor": lc.discount_factor, "time_unit_s": lc.discount_unit_s,
                     "semantics": "per-second d = factor ** (1 / time_unit_s); reward -(ns + we) per second"},
        "exploration": {"initial": lc.epsilon_initial, "floor": lc.epsilon_floor, "decay": lc.epsilon_decay,
                        "draw_order": "random() < epsilon explores, then choice(permitted)"},
        "coverage_rule": {"evaluation_requires_both_actions_updated": lc.evaluation_requires_both_actions_updated},
        "versions": {"sumo": sim.sumo_version, "traci": importlib.metadata.version("traci"),
                     "sumolib": importlib.metadata.version("sumolib"),
                     "python": f"{sys.version_info.major}.{sys.version_info.minor}"},
        "controller_schema_version": CONTROLLER_SCHEMA_VERSION,
    }


def check_compatibility(saved: dict, expected: dict, purpose: str) -> None:
    if purpose not in ("resume", "evaluate"):
        raise ValueError(f"unknown purpose {purpose!r}")
    keys = set(saved) | set(expected)
    if purpose == "evaluate":
        keys -= EVALUATION_MAY_DIFFER
    differing = sorted(k for k in keys if saved.get(k) != expected.get(k))
    if differing:
        details = "; ".join(f"{k}: saved {saved.get(k)!r}, expected {expected.get(k)!r}" for k in differing)
        raise CheckpointError(f"incompatible checkpoint for {purpose} ({', '.join(differing)}): {details}")


# ----------------------------------------------------------------------
# RNG state as JSON (never pickle)
# ----------------------------------------------------------------------

def rng_to_json(rng: random.Random) -> dict:
    version, internal, gauss = rng.getstate()
    return {"implementation": RNG_IMPLEMENTATION, "python": sys.version.split()[0],
            "state": [version, list(internal), gauss]}


def rng_from_json(data: Any) -> random.Random:
    try:
        version, internal, gauss = data["state"]
    except (TypeError, KeyError, ValueError) as exc:
        raise CheckpointError("controller_rng: state must be [version, internal, gauss]") from exc
    if data.get("implementation") != RNG_IMPLEMENTATION:
        raise CheckpointError(f"controller_rng: unsupported implementation {data.get('implementation')!r}")
    if version != 3 or not isinstance(internal, list) or len(internal) != 625:
        raise CheckpointError("controller_rng: expected version 3 with 625 state words")
    if any(isinstance(w, bool) or not isinstance(w, int) or not 0 <= w < 2 ** 32 for w in internal[:624]) \
            or isinstance(internal[624], bool) or not isinstance(internal[624], int) or not 0 <= internal[624] <= 624:
        raise CheckpointError("controller_rng: invalid state words")
    if gauss is not None and (isinstance(gauss, bool) or not isinstance(gauss, float) or not math.isfinite(gauss)):
        raise CheckpointError("controller_rng: invalid gauss_next")
    rng = random.Random()
    rng.setstate((version, tuple(internal), gauss))
    return rng


# ----------------------------------------------------------------------
# Build, validate, restore
# ----------------------------------------------------------------------

def build_checkpoint(learner: Learner, progress: Progress, training_id: str, compat: dict,
                     source_run: dict | None) -> dict:
    s = learner.schedule
    data = {
        "schema_version": SCHEMA_VERSION,
        "checkpoint_id": f"{training_id}-g{progress.save_generation:05d}",
        "training_id": training_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "compatibility": compat,
        "progress": {
            "completed_episodes": progress.completed_episodes,
            "episode_attempts": progress.episode_attempts,
            "accepted_choices": s.accepted_choices,
            "completed_updates": learner.completed_updates,
            "policy_revision": learner.table.revision,
            "save_generation": progress.save_generation,
        },
        "learning": {"alpha": learner.alpha, "epsilon": s.epsilon, "epsilon_floor": s.floor,
                     "epsilon_decay": s.decay, "discount_per_second": learner.d},
        "controller_rng": rng_to_json(learner.rng),
        "q_table": learner.table.to_json()["entries"],
        "source_run": source_run,
    }
    data["integrity_sha256"] = integrity_of(data)
    return data


def _nonneg_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CheckpointError(f"{what} must be a nonnegative integer, got {value!r}")
    return value


def validate_checkpoint(data: Any) -> None:
    if not isinstance(data, dict) or set(data) != TOP_LEVEL_KEYS:
        got = sorted(data) if isinstance(data, dict) else type(data).__name__
        raise CheckpointError(f"checkpoint keys must be exactly {sorted(TOP_LEVEL_KEYS)}, got {got}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise CheckpointError(f"unsupported schema_version {data['schema_version']!r}")
    if data["integrity_sha256"] != integrity_of(data):
        raise CheckpointError("integrity check failed: content does not match integrity_sha256 (corrupt)")

    p, lr = data["progress"], data["learning"]
    if not isinstance(p, dict) or not isinstance(lr, dict) or not isinstance(data["compatibility"], dict):
        raise CheckpointError("progress, learning, and compatibility must be objects")
    for key in ("completed_episodes", "episode_attempts", "accepted_choices", "completed_updates",
                "policy_revision", "save_generation"):
        _nonneg_int(p.get(key), f"progress.{key}")
    if p["completed_episodes"] > p["episode_attempts"]:
        raise CheckpointError("progress.completed_episodes exceeds episode_attempts")
    if p["completed_updates"] != p["policy_revision"]:
        raise CheckpointError("progress.completed_updates must equal policy_revision")
    floor, eps = lr.get("epsilon_floor"), lr.get("epsilon")
    for v, what in ((floor, "epsilon_floor"), (eps, "epsilon"), (lr.get("epsilon_decay"), "epsilon_decay"),
                    (lr.get("alpha"), "alpha"), (lr.get("discount_per_second"), "discount_per_second")):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise CheckpointError(f"learning.{what} must be finite, got {v!r}")
    if not floor <= eps <= 1.0:
        raise CheckpointError(f"learning.epsilon {eps!r} is outside [{floor}, 1.0]")
    rng_from_json(data["controller_rng"])
    try:
        table = QTable.from_json({"revision": p["policy_revision"], "entries": data["q_table"]})
    except LearningError as exc:
        raise CheckpointError(f"q_table: {exc}") from exc
    total = sum(sum(table.update_counts(s).values()) for s in table.states())
    if total != p["completed_updates"]:
        raise CheckpointError(f"q_table update counts sum to {total}, progress says {p['completed_updates']}")


def learner_from_checkpoint(config, data: dict) -> Learner:
    """Restore table, counts, exploration progress, and RNG together. Assumes validate_checkpoint passed."""
    p, lr = data["progress"], data["learning"]
    table = QTable.from_json({"revision": p["policy_revision"], "entries": data["q_table"]})
    schedule = ExplorationSchedule(lr["epsilon"], lr["epsilon_floor"], lr["epsilon_decay"], p["accepted_choices"])
    learner = Learner(config, table=table, schedule=schedule, rng=rng_from_json(data["controller_rng"]))
    learner.completed_updates = p["completed_updates"]
    return learner


def progress_from_checkpoint(data: dict) -> Progress:
    p = data["progress"]
    return Progress(p["completed_episodes"], p["episode_attempts"], p["save_generation"])


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------

def _read_valid(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CheckpointError(f"checkpoint {path} is missing") from exc
    except OSError as exc:
        raise CheckpointError(f"checkpoint {path} is unreadable: {exc}") from exc
    try:
        data = json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError as exc:
        raise CheckpointError(f"checkpoint {path} is corrupt (not strict JSON): {exc}") from exc
    try:
        validate_checkpoint(data)
    except CheckpointError as exc:
        raise CheckpointError(f"checkpoint {path} is corrupt or invalid: {exc}") from exc
    return data


class CheckpointStore:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.latest = self.directory / LATEST
        self.previous = self.directory / PREVIOUS

    def _write_temp(self, text: str, final_name: str) -> Path:
        return self._write_temp_in(self.directory, text, final_name)

    @staticmethod
    def _write_temp_in(directory: Path, text: str, final_name: str) -> Path:
        tmp = directory / f".{final_name}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "x", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    def _sync_directory(self) -> str:
        try:
            fd = os.open(self.directory, os.O_RDONLY)
        except OSError:
            return "directory fsync unsupported on this platform"
        try:
            os.fsync(fd)
            return "file and directory fsync"
        except OSError:
            return "directory fsync unsupported on this platform"
        finally:
            os.close(fd)

    def save(self, data: dict) -> SaveResult:
        """Publish `data` as latest. Raises CheckpointWriteError; never damages a valid latest on failure."""
        text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        tmp = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self._write_temp(text, LATEST)
            if _read_valid(tmp)["integrity_sha256"] != data["integrity_sha256"]:
                raise CheckpointError("re-read temporary checkpoint differs from the snapshot")
            if self.latest.exists():
                try:
                    _read_valid(self.latest)
                    keep_old = True
                except CheckpointError:
                    keep_old = False  # never rotate a corrupt latest over a valid previous
                if keep_old:
                    prev_tmp = self._write_temp(self.latest.read_text(encoding="utf-8"), PREVIOUS)
                    os.replace(prev_tmp, self.previous)
            os.replace(tmp, self.latest)
            tmp = None
        except (OSError, CheckpointError) as exc:
            raise CheckpointWriteError(f"checkpoint save to {self.latest} failed: {exc}", "failed") from exc
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        durability = self._sync_directory()
        p = data["progress"]
        return SaveResult("saved", self.latest, data["checkpoint_id"], p["save_generation"],
                          data["integrity_sha256"], file_sha256(self.latest), durability)

    def load(self, path: Path | str, expected_compat: dict, purpose: str) -> dict:
        """Validate and return a checkpoint. Never falls back to another file on its own."""
        path = Path(path)
        try:
            data = _read_valid(path)
            check_compatibility(data["compatibility"], expected_compat, purpose)
        except CheckpointError as exc:
            hint = ""
            if path.resolve() == self.latest.resolve() and self.previous.exists():
                try:
                    _read_valid(self.previous)
                    hint = (f" A valid previous checkpoint exists at {self.previous}; it is not loaded "
                            f"automatically. Select it explicitly to use it.")
                except CheckpointError:
                    pass
            raise CheckpointError(f"{exc}{hint}") from exc
        return data

    def write_milestone(self, milestone_dir: Path | str, completed_episodes: int) -> Path:
        """Immutable copy of the current valid latest as milestones/episode_NNN.json."""
        milestone_dir = Path(milestone_dir)
        path = milestone_dir / f"episode_{completed_episodes:03d}.json"
        if path.exists():
            raise CheckpointWriteError(f"milestone {path} already exists; refusing to overwrite")
        try:
            text = self.latest.read_text(encoding="utf-8")
            _read_valid(self.latest)
            milestone_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._write_temp_in(milestone_dir, text, path.name)
            try:
                os.link(tmp, path)  # publish only a complete file; fails if the name was taken meanwhile
            finally:
                tmp.unlink(missing_ok=True)
        except FileExistsError as exc:
            raise CheckpointWriteError(f"milestone {path} already exists; refusing to overwrite") from exc
        except (OSError, CheckpointError) as exc:
            raise CheckpointWriteError(f"milestone {path} could not be written: {exc}") from exc
        return path
