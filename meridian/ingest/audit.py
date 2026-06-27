"""Clock-alignment & no-lookahead audit over normalized events (ROADMAP §1, §18).

Operating principle: arrival order is never trusted, but an event can never be
*received before it happened*. For each feed we check that
    latency = ingest_time - event_time  >=  -tolerance
where tolerance absorbs source clock skew. A row violating this is a lookahead
hazard and is flagged (and, for the strict no-lookahead audit, fatal).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from ..adapters.base import NormalizedEvent

# Clock-skew tolerance: how far ingest_time may legitimately precede event_time
# purely from cross-source clock drift before we call it a violation.
CLOCK_SKEW_TOLERANCE_S = 120.0


@dataclass(frozen=True)
class SourceAlignment:
    source: str
    family: str
    count: int
    min_latency_s: float
    median_latency_s: float
    max_latency_s: float
    violations: int  # rows with latency < -tolerance (received before it happened)


def _median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def alignment_report(
    events: Iterable[NormalizedEvent], tolerance_s: float = CLOCK_SKEW_TOLERANCE_S
) -> list[SourceAlignment]:
    """Per (source, family): latency distribution + lookahead-violation count."""
    buckets: dict[tuple[str, str], list[float]] = {}
    for e in events:
        buckets.setdefault((e.source, e.family), []).append(e.latency_seconds)
    out: list[SourceAlignment] = []
    for (source, family), lats in sorted(buckets.items()):
        violations = sum(1 for lat in lats if lat < -tolerance_s)
        out.append(
            SourceAlignment(
                source=source,
                family=family,
                count=len(lats),
                min_latency_s=min(lats),
                median_latency_s=_median(lats),
                max_latency_s=max(lats),
                violations=violations,
            )
        )
    return out


def lookahead_violations(
    events: Iterable[NormalizedEvent], tolerance_s: float = CLOCK_SKEW_TOLERANCE_S
) -> list[NormalizedEvent]:
    """Events received meaningfully before they happened (ingest_time << event_time)."""
    return [e for e in events if e.latency_seconds < -tolerance_s and not math.isnan(e.latency_seconds)]
