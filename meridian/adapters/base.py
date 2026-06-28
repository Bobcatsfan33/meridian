"""Common Adapter interface + canonical event dataclasses (ROADMAP §6, §7).

Design split (so the network layer never touches test determinism):
  - `fetch(date, ctx)`   -> raw payloads from the source (network; not unit-tested)
  - `normalize(raw, ctx)`-> canonical NormalizedEvent rows (PURE; golden-file tested)

No thresholds live here. Per the hard rules all grading/abnormality is Layer-1
featurization (Phase 2); ingestion only records *what objectively happened* with
dual timestamps. `confidence` is data-source reliability, not a data threshold.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from ..ingest.clock import naive_utc, to_utc

# Canonical event families (ROADMAP §7). `earnings` extends the §7 list because
# earnings is a first-class driver in §6 with no home among the original nine.
FAMILIES = frozenset(
    {
        "price_volume",
        "options_flow",
        "dealer_pos",
        "news",
        "filing",
        "sector_peer",
        "macro",
        "liquidity",
        "attention",
        "earnings",
        "equity_flow",   # FINRA short-volume + dark-pool (off-exchange) flow (Part B)
    }
)


@dataclass(frozen=True)
class IngestContext:
    """Read-only inputs handed to every adapter for one ingest run.

    `now` is the injected wall clock so ingest_time is deterministic in tests.
    """

    trade_date: dt.date
    now: dt.datetime  # tz-aware; the run's ingest_time anchor
    universe: tuple[dict[str, str], ...] = ()  # rows: symbol,name,sector,index_membership
    etfs: tuple[dict[str, str], ...] = ()  # rows: symbol,role,description
    settings: dict[str, Any] = field(default_factory=dict)  # this adapter's config block

    @property
    def universe_symbols(self) -> tuple[str, ...]:
        return tuple(r["symbol"] for r in self.universe)

    def sector_of(self, symbol: str) -> str | None:
        for r in self.universe:
            if r["symbol"] == symbol:
                return r.get("sector") or None
        return None


@dataclass(frozen=True)
class RawEvent:
    """A single payload exactly as pulled from a source, plus arrival time."""

    source: str
    ingest_time: dt.datetime  # tz-aware UTC; when WE received it
    ticker: str | None
    payload: dict[str, Any]

    @property
    def raw_id(self) -> str:
        """Deterministic id from content (excludes ingest_time) so re-runs upsert."""
        digest = _stable_digest(self.source, self.ticker, self.payload)
        return f"raw_{digest}"


@dataclass(frozen=True)
class NormalizedEvent:
    """Canonical typed event (ROADMAP §7). abnormality/regime_tags are filled in L1."""

    event_id: str
    event_time: dt.datetime  # tz-aware UTC; when it happened (aligned)
    ingest_time: dt.datetime  # tz-aware UTC; when we received it
    ticker: str | None
    event_type: str
    family: str
    source: str
    confidence: float  # data-quality / source reliability in [0,1]
    sector: str | None = None
    related_symbols: tuple[str, ...] = ()
    parent_event_id: str | None = None  # reserved (complex-event membership)
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"unknown event family: {self.family!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of [0,1]: {self.confidence!r}")

    @property
    def latency_seconds(self) -> float:
        """ingest_time - event_time. Negative => received before it happened (lookahead)."""
        return (to_utc(self.ingest_time) - to_utc(self.event_time)).total_seconds()

    def as_storage_row(self) -> dict[str, Any]:
        """Flatten to the normalized_events column shape (timestamps -> naive UTC)."""
        return {
            "event_id": self.event_id,
            "event_time": naive_utc(self.event_time),
            "ingest_time": naive_utc(self.ingest_time),
            "ticker": self.ticker,
            "event_type": self.event_type,
            "family": self.family,
            "source": self.source,
            "confidence": self.confidence,
            "sector": self.sector,
            "related_symbols": list(self.related_symbols),
            "parent_event_id": self.parent_event_id,
            "payload": self.payload,
        }


class Adapter(ABC):
    """Common feed interface. Subclasses set name/source/family/reliability/latency."""

    name: str = "adapter"
    source: str = "adapter"
    default_family: str = "price_volume"
    reliability: float = 0.9  # data-quality confidence assigned to emitted events
    # Expected/typical feed latency (s): how long after event_time we plausibly
    # receive the row. Used by the clock-alignment audit as the tolerance band.
    expected_latency_seconds: float = 60.0
    priority: int = 1  # 1=free, 2=robinhood, 3=paid (ROADMAP §6)

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    @abstractmethod
    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        """Pull raw payloads for `ctx.trade_date` (network). Never raise on partial
        feed failure — return what was retrieved so coverage can be reported."""

    @abstractmethod
    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        """Pure transform of one raw payload into >=0 canonical events."""

    def run(self, ctx: IngestContext) -> tuple[list[RawEvent], list[NormalizedEvent]]:
        """fetch + normalize. Default orchestration; adapters rarely override."""
        raws = self.fetch(ctx)
        events: list[NormalizedEvent] = []
        for raw in raws:
            events.extend(self.normalize(raw, ctx))
        return raws, events

    # convenience for subclasses ------------------------------------------------
    def _event(
        self,
        *,
        event_type: str,
        event_time: dt.datetime,
        ingest_time: dt.datetime,
        ticker: str | None,
        payload: dict[str, Any],
        family: str | None = None,
        sector: str | None = None,
        related_symbols: Iterable[str] = (),
        id_extra: str = "",
    ) -> NormalizedEvent:
        fam = family or self.default_family
        eid = make_event_id(self.source, event_type, ticker, to_utc(event_time), id_extra)
        return NormalizedEvent(
            event_id=eid,
            event_time=to_utc(event_time),
            ingest_time=to_utc(ingest_time),
            ticker=ticker,
            event_type=event_type,
            family=fam,
            source=self.source,
            confidence=self.reliability,
            sector=sector,
            related_symbols=tuple(related_symbols),
            payload=payload,
        )


def make_event_id(
    source: str, event_type: str, ticker: str | None, event_time: dt.datetime, extra: str = ""
) -> str:
    """Stable id keyed on identity (not ingest_time) so re-ingest upserts in place."""
    key = f"{source}|{event_type}|{ticker}|{event_time.isoformat()}|{extra}"
    return "evt_" + hashlib.sha1(key.encode()).hexdigest()[:20]


def _stable_digest(*parts: Any) -> str:
    import json

    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:20]


__all__ = [
    "Adapter",
    "IngestContext",
    "RawEvent",
    "NormalizedEvent",
    "make_event_id",
    "FAMILIES",
    "replace",
]
