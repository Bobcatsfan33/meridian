"""No-lookahead + clock-alignment audit tests (ROADMAP §18)."""
from __future__ import annotations

import datetime as dt

from tests.conftest import NOW

from meridian.adapters.base import NormalizedEvent
from meridian.ingest.audit import alignment_report, lookahead_violations, CLOCK_SKEW_TOLERANCE_S
from meridian.ingest.clock import UTC


def _evt(event_time, ingest_time, source="s", family="price_volume") -> NormalizedEvent:
    return NormalizedEvent(
        event_id="evt_x" + str(event_time.timestamp()),
        event_time=event_time,
        ingest_time=ingest_time,
        ticker="AAPL",
        event_type="DailyBar",
        family=family,
        source=source,
        confidence=0.9,
    )


def test_normal_event_no_violation():
    e = _evt(dt.datetime(2026, 6, 26, 20, 0, tzinfo=UTC), NOW)
    assert e.latency_seconds > 0  # received after it happened
    assert lookahead_violations([e]) == []


def test_lookahead_event_flagged():
    # ingest_time precedes event_time by an hour -> received before it happened.
    event_time = dt.datetime(2026, 6, 26, 20, 0, tzinfo=UTC)
    ingest_time = event_time - dt.timedelta(hours=1)
    e = _evt(event_time, ingest_time)
    assert e.latency_seconds < -CLOCK_SKEW_TOLERANCE_S
    assert len(lookahead_violations([e])) == 1


def test_within_skew_tolerance_ok():
    event_time = dt.datetime(2026, 6, 26, 20, 0, tzinfo=UTC)
    ingest_time = event_time - dt.timedelta(seconds=CLOCK_SKEW_TOLERANCE_S / 2)
    assert lookahead_violations([_evt(event_time, ingest_time)]) == []


def test_alignment_report_groups_by_source_family():
    e1 = _evt(dt.datetime(2026, 6, 26, 20, 0, tzinfo=UTC), NOW, source="yfinance")
    e2 = _evt(dt.datetime(2026, 6, 26, 20, 0, tzinfo=UTC), NOW, source="fred", family="macro")
    rep = {(a.source, a.family): a for a in alignment_report([e1, e2])}
    assert ("yfinance", "price_volume") in rep
    assert ("fred", "macro") in rep
    assert rep[("yfinance", "price_volume")].count == 1
    assert rep[("yfinance", "price_volume")].violations == 0
