"""Typed immutable records and enums shared across modules (build contract, Section D).

Records are added by the chunk that first produces them, so no field is
guessed ahead of the module that defines it.
"""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    NS = "NS"
    WE = "WE"

    @property
    def other(self) -> "Direction":
        return Direction.WE if self is Direction.NS else Direction.NS


class Mode(str, Enum):
    TRAIN = "train"
    EVALUATE = "evaluate"


class SignalPhase(str, Enum):
    NS_GREEN = "NS_GREEN"
    NS_YELLOW = "NS_YELLOW"
    WE_GREEN = "WE_GREEN"
    WE_YELLOW = "WE_YELLOW"
