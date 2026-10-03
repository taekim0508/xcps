"""Minimal stand-in for the chunk-2 runtime: Section E steps 1 to 3 for one second.

Tests drive the signal directly through the adapter; there is no controller yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from traffic_cps.adapter import SumoAdapter
from traffic_cps.config import load_config
from traffic_cps.safety import SafetyMonitor
from traffic_cps.scenario import DemandProfile, create_scenario
from traffic_cps.sensor import TrafficSensor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_config(PROJECT_ROOT / "configs" / "default.yaml")


def make_scenario(network_dir: Path, out: Path, seed=0, profile="balanced", horizon=120) -> Path:
    create_scenario(network_dir, out, seed, DemandProfile(profile), horizon)
    return out


@dataclass
class Harness:
    adapter: SumoAdapter
    monitor: SafetyMonitor
    sensor: TrafficSensor
    observations: list = field(default_factory=list)
    events: list = field(default_factory=list)
    violations: list = field(default_factory=list)

    @classmethod
    def start(cls, scenario: Path, horizon=120, connection_wrapper=None) -> "Harness":
        monitor = SafetyMonitor(
            yellow_s=CONFIG.signal.yellow_s,
            minimum_green_s=CONFIG.signal.minimum_green_s,
            green_service_limit_s=CONFIG.signal.green_service_limit_s,
            initial_green=CONFIG.simulation.initial_green,
        )
        adapter = SumoAdapter(CONFIG, monitor, connection_wrapper=connection_wrapper)
        adapter.start(scenario, horizon_s=horizon)
        sensor = TrafficSensor(
            CONFIG.network.lanes_by_direction(), adapter.lane_lengths,
            CONFIG.sensor.vehicle_length_m, CONFIG.sensor.minimum_gap_m, CONFIG.sensor.retries,
        )
        h = cls(adapter, monitor, sensor)
        h.observations.append(h._observe())
        return h

    def _observe(self):
        raw, quality = self.sensor.read(self.adapter.read_raw, self.adapter.time)
        return self.sensor.observe(raw, self.adapter.timing(), quality)

    def tick(self):
        """Advance one second; read and monitor; complete a due yellow; observe."""
        t = self.adapter.advance_one_second()
        raw, quality = self.sensor.read(self.adapter.read_raw, t)
        self.violations += self.monitor.observe(raw.signal_state, t, "tick")
        self.adapter.confirm(raw)
        done = self.adapter.complete_due_transition()
        if done is not None:
            self.events.append(done)
            self.violations += list(done.violations)
        obs = self.sensor.observe(raw, self.adapter.timing(), quality)
        self.observations.append(obs)
        return obs

    def run_until(self, t_end):
        while self.adapter.time < t_end:
            self.tick()
        return self.observations[-1]

    def close(self):
        self.adapter.close()
