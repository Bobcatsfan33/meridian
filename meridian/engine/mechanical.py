"""Mechanical-vs-informational classifier (ROADMAP §2, §13).

When dealer-positioning evidence is present and a price move expands, the move is
mechanical (dealer hedging) and any same-session headline is demoted to LATE
confirmation rather than the cause. Without positioning evidence but with a news/filing
catalyst, the move is informational. Pure & golden-tested.
"""
from __future__ import annotations

from dataclasses import dataclass

from .structural import MatchEvent

_DEALER_TYPES = {"ShortGamma", "SpotIntoStrike", "GammaFlip"}


@dataclass(frozen=True)
class MoveClass:
    label: str           # mechanical | informational | mixed
    demote_news: bool
    reason: str


def classify(bindings: dict[str, MatchEvent | None], catalysts: list[MatchEvent]) -> MoveClass:
    has_positioning = any(
        ev is not None and ev.event_type in _DEALER_TYPES for ev in bindings.values()
    )
    has_catalyst = any(c.family in ("news", "filing") for c in catalysts)
    if has_positioning and has_catalyst:
        return MoveClass("mechanical", True,
                         "Dealer-positioning evidence present; headline demoted to late confirmation.")
    if has_positioning:
        return MoveClass("mechanical", False, "Dealer-hedging driven; no competing catalyst.")
    if has_catalyst:
        return MoveClass("informational", False, "News/filing catalyst present; treated as informational.")
    return MoveClass("mixed", False, "No decisive mechanical or informational signature.")
