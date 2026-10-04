"""Fault injection for tests, and evaluation-only sensor noise for the robustness experiment.

`FaultyConnection` wraps a real TraCI connection: SUMO still runs for real and
only the calls a test arms misbehave. Nothing here is used by normal runs.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any, Callable, Mapping

from traffic_cps.contracts import SafetyOverride, SafetyVerdict

_DOMAINS = ("simulation", "trafficlight", "lane", "vehicle")


class InjectedFault(RuntimeError):
    """Raised by an armed fault in place of a real TraCI call."""


class FaultyConnection:
    def __init__(self, inner: Any):
        self.inner = inner
        self._fail: Counter = Counter()
        self._returns: dict[tuple[str, str], list[Any]] = {}
        self._substitute_mask: str | None = None
        self._fail_readback_after_write = False
        self._calls: Counter = Counter()
        self._steps = 0
        self._at_step: dict[int, list[Callable[[], None]]] = {}

    # Arming ---------------------------------------------------------------

    def fail_next(self, domain: str, method: str, times: int = 1) -> None:
        """Make the next `times` calls of domain.method raise InjectedFault."""
        self._fail[(domain, method)] += times

    def return_next(self, domain: str, method: str, value: Any, times: int = 1) -> None:
        """Make the next `times` calls of domain.method return `value` instead of the real result."""
        self._returns.setdefault((domain, method), []).extend([value] * times)

    def substitute_next_signal_write(self, mask: str) -> None:
        """The next signal write sends `mask` to SUMO instead of what the adapter asked for."""
        self._substitute_mask = mask

    def fail_readback_after_next_write(self) -> None:
        """The read-back that follows the next signal write raises (the command becomes unconfirmed)."""
        self._fail_readback_after_write = True

    def at_step(self, step: int, action: Callable[[], None]) -> None:
        """Run `action` (usually an arming call) right after the `step`-th simulationStep returns."""
        self._at_step.setdefault(step, []).append(action)

    def calls(self, domain: str, method: str) -> int:
        return self._calls[(domain, method)]

    # Proxying -------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name == "simulationStep":
            return self._step
        return _DomainProxy(self, name, attr) if name in _DOMAINS else attr

    def _step(self, *args: Any, **kwargs: Any) -> Any:
        """simulationStep, armable as ("connection", "simulationStep")."""
        value = self._invoke("connection", "simulationStep", self.inner.simulationStep, args, kwargs)
        self._steps += 1
        for action in self._at_step.pop(self._steps, []):
            action()
        return value

    def _invoke(self, domain: str, method: str, fn: Any, args: tuple, kwargs: dict) -> Any:
        key = (domain, method)
        self._calls[key] += 1
        if self._fail[key] > 0:
            self._fail[key] -= 1
            raise InjectedFault(f"injected failure in {domain}.{method}")
        if self._returns.get(key):
            return self._returns[key].pop(0)
        if key == ("trafficlight", "setRedYellowGreenState") and self._substitute_mask is not None:
            args = (args[0], self._substitute_mask, *args[2:])
            self._substitute_mask = None
        if key == ("trafficlight", "setRedYellowGreenState") and self._fail_readback_after_write:
            self._fail_readback_after_write = False
            self._fail[("trafficlight", "getRedYellowGreenState")] += 1
        return fn(*args, **kwargs)


class _DomainProxy:
    def __init__(self, owner: FaultyConnection, domain: str, inner: Any):
        self._owner, self._domain, self._inner = owner, domain, inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            return self._owner._invoke(self._domain, name, attr, args, kwargs)

        return wrapped


def disable_adapter_timing_guard(adapter: Any) -> None:
    """Simulate an adapter bug that skips its own minimum-green check (tests only)."""
    adapter._enforce_timing = False


def veto_write_from(monitor: Any, origin_id: str, invariant: str = "S2") -> None:
    """Make the monitor veto the next write from `origin_id` (simulates an unexpected block; tests only)."""
    real = monitor.check_proposed
    armed = {"on": True}

    def check_proposed(mask: str, time: int, origin: Any) -> SafetyVerdict:
        if armed["on"] and origin.origin_id == origin_id:
            armed["on"] = False
            reason = "injected veto for a test"
            monitor.overrides.append(SafetyOverride(time, mask, monitor._mask, origin, invariant, reason))
            return SafetyVerdict(False, invariant, reason)
        return real(mask, time, origin)

    monitor.check_proposed = check_proposed


# ----------------------------------------------------------------------
# Evaluation-only sensor noise (build contract Section H)
# ----------------------------------------------------------------------

class SensorNoise:
    """Valid-looking but wrong lane counts for the labeled robustness experiment.

    Applied once to each successful primary reading (never to make a retry succeed). At t=0 the
    true count is delivered. Afterwards, for each lane in fixed order, two uniforms are always
    drawn: the first decides staleness (reuse the previous delivered count), the second becomes
    integer noise floor(u * (2b + 1)) - b in {-b..b}; otherwise max(0, true + noise) is delivered.
    Using exactly two random() draws per lane per second makes every level consume an identical
    draw sequence for the same seed (randint would not guarantee that).
    """

    def __init__(self, label: str, noise_bound_vehicles: int, stale_probability: float, seed: int,
                 lane_order: tuple[str, ...]):
        if isinstance(noise_bound_vehicles, bool) or not isinstance(noise_bound_vehicles, int) \
                or noise_bound_vehicles < 0:
            raise ValueError(f"noise bound must be a nonnegative integer, got {noise_bound_vehicles!r}")
        if not 0.0 <= stale_probability <= 1.0:
            raise ValueError(f"stale probability must be in [0, 1], got {stale_probability!r}")
        self.label = label
        self.bound = noise_bound_vehicles
        self.stale_probability = stale_probability
        self.seed = seed
        self.lane_order = tuple(lane_order)
        self.rng = random.Random(seed)
        self.previous: dict[str, int] | None = None
        self.last_time: int | None = None
        self.draws = 0

    def describe(self) -> dict:
        return {"label": self.label, "noise_bound_vehicles": self.bound,
                "stale_probability": self.stale_probability, "seed": self.seed,
                "lane_order": list(self.lane_order), "draws_per_lane_per_second": 2}

    def measure(self, t: int, true_counts: Mapping[str, int]) -> dict[str, int]:
        expected = 0 if self.last_time is None else self.last_time + 1
        if t != expected:
            raise ValueError(f"sensor noise expects consecutive seconds: got t={t}, expected t={expected}")
        self.last_time = t
        if t == 0:
            delivered = {lane: true_counts[lane] for lane in self.lane_order}
        else:
            delivered = {}
            for lane in self.lane_order:
                u_stale, u_noise = self.rng.random(), self.rng.random()
                self.draws += 2
                noise = math.floor(u_noise * (2 * self.bound + 1)) - self.bound
                if u_stale < self.stale_probability:
                    delivered[lane] = self.previous[lane]
                else:
                    delivered[lane] = max(0, true_counts[lane] + noise)
        self.previous = dict(delivered)
        return delivered


def service_demand_fault(true_competing: int, measured_competing: int, elapsed_green_s: int | None,
                         service_limit_s: int, forced: bool) -> str | None:
    """Classify a service-limit outcome caused by sensing error (Module 8).

    phantom_demand: a forced switch happened while the true competing count was zero.
    masked_demand: at or beyond the limit the true competing count was positive but the measured
    count was zero, so no switch was forced. Both are evaluation diagnostics, not safety violations.
    """
    if forced:
        return "phantom_demand" if true_competing == 0 and measured_competing > 0 else None
    if elapsed_green_s is not None and elapsed_green_s >= service_limit_s \
            and true_competing > 0 and measured_competing == 0:
        return "masked_demand"
    return None
