"""Controllers choose a requested direction; they never actuate or advance time."""

from __future__ import annotations

from typing import Protocol

from traffic_cps.contracts import Decision, DecisionContext


class Controller(Protocol):
    name: str

    def select(self, context: DecisionContext, decision_id: str) -> Decision:
        """Return a decision for a free choice. Called only when timing allows a choice."""
        ...
