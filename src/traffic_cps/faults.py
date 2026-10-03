"""Fault injection for tests (this chunk) and evaluation-only sensor noise (chunk 7).

`FaultyConnection` wraps a real TraCI connection: SUMO still runs for real and
only the calls a test arms misbehave. Nothing here is used by normal runs.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

_DOMAINS = ("simulation", "trafficlight", "lane", "vehicle")


class InjectedFault(RuntimeError):
    """Raised by an armed fault in place of a real TraCI call."""


class FaultyConnection:
    def __init__(self, inner: Any):
        self.inner = inner
        self._fail: Counter = Counter()
        self._returns: dict[tuple[str, str], list[Any]] = {}
        self._substitute_mask: str | None = None
        self._calls: Counter = Counter()

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

    def calls(self, domain: str, method: str) -> int:
        return self._calls[(domain, method)]

    # Proxying -------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        return _DomainProxy(self, name, attr) if name in _DOMAINS else attr

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
