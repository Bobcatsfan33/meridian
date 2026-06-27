"""Clock alignment: every timestamp normalizes to tz-aware UTC.

Operating principle #3 (ROADMAP §1): events carry event_time AND ingest_time and
correlation uses event-time *after clock alignment*; arrival order is never trusted.
All adapters route their timestamps through here so the engine sees one clock (UTC).
Storage convention: DuckDB TIMESTAMP columns hold naive UTC (tz stripped at write).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
MARKET_TZ = ZoneInfo("America/New_York")

# US regular-session close, in the exchange's local wall clock.
MARKET_CLOSE_LOCAL = dt.time(16, 0)
MARKET_OPEN_LOCAL = dt.time(9, 30)


def to_utc(when: dt.datetime, assume_tz: ZoneInfo | dt.timezone = UTC) -> dt.datetime:
    """Return a tz-aware UTC datetime. Naive inputs are assumed to be `assume_tz`."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=assume_tz)
    return when.astimezone(UTC)


def market_close_utc(trade_date: dt.date) -> dt.datetime:
    """16:00 America/New_York on `trade_date`, expressed in UTC.

    Uses zoneinfo so EST/EDT offset (UTC-5 vs UTC-4) is handled correctly rather
    than hard-coded — daily bars then align to the true session close.
    """
    local = dt.datetime.combine(trade_date, MARKET_CLOSE_LOCAL, tzinfo=MARKET_TZ)
    return local.astimezone(UTC)


def market_time_utc(trade_date: dt.date, local_time: dt.time) -> dt.datetime:
    """An arbitrary local wall-clock time on `trade_date`, expressed in UTC."""
    local = dt.datetime.combine(trade_date, local_time, tzinfo=MARKET_TZ)
    return local.astimezone(UTC)


def naive_utc(when: dt.datetime) -> dt.datetime:
    """Strip tz after converting to UTC — the on-disk storage form."""
    return to_utc(when).replace(tzinfo=None)


def parse_date(value: str) -> dt.date:
    """Parse an ISO YYYY-MM-DD date (CLI input)."""
    return dt.date.fromisoformat(value)
