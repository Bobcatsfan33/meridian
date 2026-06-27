"""Layer-2 structural matching (ROADMAP §9). Poset-native partial scoring.

Operators precedes / concurrent / independent / contradicts over event-time (after
clock alignment — arrival order is never used). A match returns a graded completeness
in [0,1] (matched legs weighted by abnormality), NEVER a boolean. Pure & golden-tested.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MatchEvent:
    event_id: str
    event_time: dt.datetime
    ticker: str | None
    family: str
    event_type: str
    abnormality: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchWindows:
    concurrent_window_s: float = 86400.0   # daily granularity: same session
    precedes_min_lag_s: float = 0.0
    precedes_max_window_s: float = 86400.0


@dataclass(frozen=True)
class RelationEdge:
    src: MatchEvent
    dst: MatchEvent
    observed_relation: str   # precedes | concurrent | contradicts
    lag_seconds: float
    score: float


# --- poset operators (pure) ------------------------------------------------------
def precedes(a: MatchEvent, b: MatchEvent, w: MatchWindows) -> bool:
    lag = (b.event_time - a.event_time).total_seconds()
    return lag >= w.precedes_min_lag_s and 0 < lag <= w.precedes_max_window_s


def concurrent(a: MatchEvent, b: MatchEvent, w: MatchWindows) -> bool:
    return abs((a.event_time - b.event_time).total_seconds()) <= w.concurrent_window_s


def independent(a: MatchEvent, b: MatchEvent, w: MatchWindows) -> bool:
    return not concurrent(a, b, w) and not precedes(a, b, w) and not precedes(b, a, w)


def contradicts(a: MatchEvent, b: MatchEvent) -> bool:
    """B runs against what A implies. Phase-3 proxy: a price move opposite to a
    headline's sentiment sign (sentiment available later); returns False without it."""
    sa = a.payload.get("sentiment")
    rb = b.payload.get("ret_1m")
    if sa is None or rb is None:
        return False
    return (sa > 0 and rb < 0) or (sa < 0 and rb > 0)


# --- pattern evaluation ----------------------------------------------------------
@dataclass
class MatchResult:
    completeness: float
    leg_scores: list[float] = field(default_factory=list)
    edges: list[RelationEdge] = field(default_factory=list)
    bindings: dict[str, MatchEvent | None] = field(default_factory=dict)


def evaluate(pattern, bindings: dict[str, MatchEvent | None],
             present_families: set[str], w: MatchWindows) -> MatchResult:
    scores: list[float] = []
    edges: list[RelationEdge] = []
    for leg in pattern.legs:
        s, edge = _leg_score(leg, bindings, present_families, w)
        scores.append(s)
        if edge is not None:
            edges.append(edge)
    completeness = sum(scores) / len(scores) if scores else 0.0
    return MatchResult(completeness=completeness, leg_scores=scores, edges=edges, bindings=bindings)


def _leg_score(leg, bindings, present_families, w) -> tuple[float, RelationEdge | None]:
    if leg.type == "present":
        e = bindings.get(leg.role)
        return (e.abnormality if e else 0.0), None
    if leg.type == "absent":
        return (1.0 if leg.family not in present_families else 0.0), None
    if leg.type == "feature":
        e = bindings.get(leg.role)
        if not e:
            return 0.0, None
        v = e.payload.get(leg.feature)
        return (float(v) if isinstance(v, (int, float)) else 0.0), None
    if leg.type in ("concurrent", "precedes", "contradicts"):
        a, b = bindings.get(leg.a), bindings.get(leg.b)
        if not a or not b:
            return 0.0, None
        ok = (
            concurrent(a, b, w) if leg.type == "concurrent"
            else precedes(a, b, w) if leg.type == "precedes"
            else contradicts(a, b)
        )
        if not ok:
            return 0.0, None
        score = min(a.abnormality, b.abnormality)
        lag = (b.event_time - a.event_time).total_seconds()
        return score, RelationEdge(a, b, leg.type, lag, score)
    return 0.0, None
