"""Independent safety monitor (Module 4A).

The monitor checks every proposed low-level signal write (pre-command) and
every observed raw signal string (post-observation) against invariants S1 to
S4. It keeps its own color history built only from raw strings it is shown,
and decodes masks with its own code, so a bug in the adapter's timers or mask
decoding cannot hide a violation. It never selects actions, retries commands,
or advances the simulator.

  S1  At most one direction is non-red at any time.
  S2  Except for the validated t=0 initialization, a direction becomes green only
      after the opposing direction showed yellow for >= yellow_s and turns red in
      the same change. Greens end via yellow; yellows end with the opposing green.
  S3  No green ends (turns yellow) before minimum green has elapsed.
  S4  Before the horizon, a green at or beyond the service limit with measured
      competing demand must show yellow by the next observed second.
"""

from __future__ import annotations

from typing import Iterable

from traffic_cps.contracts import (
    Direction,
    Observation,
    Origin,
    OriginKind,
    SafetyOverride,
    SafetyVerdict,
    SafetyViolation,
    SignalColor,
)

RED, YELLOW, GREEN = SignalColor.RED, SignalColor.YELLOW, SignalColor.GREEN
_CHAR_COLORS = {"G": GREEN, "g": GREEN, "y": YELLOW, "r": RED}


class SafetyMonitor:
    def __init__(
        self,
        yellow_s: int,
        minimum_green_s: int,
        green_service_limit_s: int,
        initial_green: Direction,
        index_directions: Iterable[Direction] | None = None,
        horizon_s: int | None = None,
    ):
        self.yellow_s = yellow_s
        self.minimum_green_s = minimum_green_s
        self.green_service_limit_s = green_service_limit_s
        self.initial_green = Direction(initial_green)
        self.index_directions: tuple[Direction, ...] | None = None
        self.horizon_s: int | None = None
        if index_directions is not None or horizon_s is not None:
            self.bind(tuple(index_directions or ()), horizon_s)

        self._initialization_used = False
        self._mask: str | None = None
        self._colors: dict[Direction, SignalColor] = {}
        self._since: dict[Direction, int] = {}
        self._service_due_since: int | None = None
        self.overrides: list[SafetyOverride] = []
        self.violations: list[SafetyViolation] = []

    def bind(self, index_directions: tuple[Direction, ...], horizon_s: int | None) -> None:
        """Attach the validated signal-index mapping and run horizon (once, before any write)."""
        if self.index_directions is not None:
            raise RuntimeError("safety monitor is already bound")
        if not index_directions or set(index_directions) != set(Direction):
            raise ValueError(f"index mapping must cover both directions, got {index_directions!r}")
        if not isinstance(horizon_s, int) or horizon_s <= 0:
            raise ValueError(f"horizon must be a positive integer, got {horizon_s!r}")
        self.index_directions = tuple(index_directions)
        self.horizon_s = horizon_s

    # ------------------------------------------------------------------
    # Mask decoding (deliberately independent of the adapter's)
    # ------------------------------------------------------------------

    def _decode(self, mask: str) -> dict[Direction, SignalColor] | None:
        if self.index_directions is None:
            raise RuntimeError("safety monitor used before bind()")
        if not isinstance(mask, str) or len(mask) != len(self.index_directions):
            return None
        seen: dict[Direction, set[SignalColor]] = {d: set() for d in Direction}
        for char, direction in zip(mask, self.index_directions):
            color = _CHAR_COLORS.get(char)
            if color is None:
                return None
            seen[direction].add(color)
        if any(len(colors) != 1 for colors in seen.values()):
            return None  # one direction showing two colors at once
        return {d: next(iter(colors)) for d, colors in seen.items()}

    def _initial_pattern(self) -> dict[Direction, SignalColor]:
        return {self.initial_green: GREEN, self.initial_green.other: RED}

    def _change_problems(self, new: dict[Direction, SignalColor], t: int) -> list[tuple[str, str]]:
        """(invariant, reason) for every rule a change from the current history would break."""
        problems: list[tuple[str, str]] = []
        non_red = [d.value for d in Direction if new[d] is not RED]
        if len(non_red) > 1:
            problems.append(("S1", f"both directions non-red ({', '.join(non_red)})"))
        for d in Direction:
            old, now = self._colors[d], new[d]
            if old is now:
                continue
            o = d.other
            if (old, now) == (RED, GREEN):
                yellow_for = t - self._since[o] if self._colors[o] is YELLOW else None
                if yellow_for is None or yellow_for < self.yellow_s or new[o] is not RED:
                    problems.append(("S2", f"{d.value} turned green without {o.value} showing yellow for "
                                           f"{self.yellow_s}s then red (yellow for {yellow_for})"))
            elif (old, now) == (GREEN, YELLOW):
                green_for = t - self._since[d]
                if green_for < self.minimum_green_s:
                    problems.append(("S3", f"{d.value} green ended after {green_for}s "
                                           f"(minimum {self.minimum_green_s}s)"))
            elif (old, now) == (YELLOW, RED):
                if new[o] is not GREEN:
                    problems.append(("S2", f"{d.value} yellow ended without {o.value} turning green"))
            else:
                problems.append(("S2", f"{d.value} changed {old.value} -> {now.value}, which is not permitted"))
        return problems

    # ------------------------------------------------------------------
    # Pre-command check
    # ------------------------------------------------------------------

    def check_proposed(self, mask: str, time: int, origin: Origin) -> SafetyVerdict:
        """Allow or veto a low-level write before it reaches the simulator."""

        def veto(invariant: str, reason: str) -> SafetyVerdict:
            self.overrides.append(SafetyOverride(time, mask, self._mask, origin, invariant, reason))
            return SafetyVerdict(False, invariant, reason)

        new = self._decode(mask)
        if new is None:
            return veto("MASK", f"invalid or mixed signal mask {mask!r}")

        if origin.kind is OriginKind.INITIALIZATION or self._mask is None:
            if self._initialization_used:
                return veto("S2", "initialization exception already used")
            if self._mask is not None or time != 0 or origin.kind is not OriginKind.INITIALIZATION:
                return veto("S2", "only the configured initialization write at t=0 may skip the transition rules")
            if new != self._initial_pattern():
                return veto("S2", f"initialization must set {self.initial_green.value} green, the other red")
            self._initialization_used = True
            return SafetyVerdict(True, None, "validated initialization")

        problems = self._change_problems(new, time)
        if problems:
            return veto(*problems[0])
        return SafetyVerdict(True, None, "allowed")

    # ------------------------------------------------------------------
    # Post-observation check
    # ------------------------------------------------------------------

    def observe(self, raw_signal: str, time: int, stage: str) -> list[SafetyViolation]:
        """Check one observed raw signal string and update the monitor's own history."""
        previous = self._mask
        new = self._decode(raw_signal)
        found: list[SafetyViolation] = []

        def violation(invariant: str, detail: str) -> None:
            found.append(SafetyViolation(time, invariant, raw_signal, previous, stage, detail))

        if new is None:
            violation("MASK", f"invalid or mixed signal mask {raw_signal!r}")
        elif previous is None:
            if not (self._initialization_used and time == 0 and new == self._initial_pattern()):
                violation("S2", "first observed signal state is not the validated initialization")
            else:
                self._mask, self._colors = raw_signal, new
                self._since = {d: time for d in Direction}
        else:
            by_invariant: dict[str, list[str]] = {}
            if raw_signal != previous:
                for invariant, reason in self._change_problems(new, time):
                    by_invariant.setdefault(invariant, []).append(reason)
            for invariant, reasons in by_invariant.items():
                violation(invariant, "; ".join(reasons))
            for d in Direction:
                if new[d] is not self._colors[d]:
                    self._since[d] = time
            self._mask, self._colors = raw_signal, new

        self.violations += found
        return found

    def check_service_deadline(self, observation: Observation) -> list[SafetyViolation]:
        """S4: uses measured competing demand and the monitor's own green onset."""
        t = observation.time
        if self.horizon_s is None:
            raise RuntimeError("safety monitor used before bind()")
        greens = [d for d in Direction if self._colors.get(d) is GREEN]
        if t >= self.horizon_s or len(greens) != 1:
            self._service_due_since = None
            return []
        green = greens[0]
        elapsed = t - self._since[green]
        competing = observation.counts_by_direction[green.other]
        if elapsed < self.green_service_limit_s or competing <= 0:
            self._service_due_since = None
            return []
        if self._service_due_since is None:
            self._service_due_since = t
            return []
        if t < self._service_due_since + 1:
            return []
        found = [SafetyViolation(
            t, "S4", self._mask or "", self._mask, "service_deadline",
            f"{green.value} green for {elapsed}s with {competing} measured competing vehicles; "
            f"switch was due by t={self._service_due_since + 1}",
        )]
        self.violations += found
        return found
