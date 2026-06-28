"""Step A1: Yahoo per-symbol RSS — pubDate->event_time (UTC), ticker mapping, dedup."""
from __future__ import annotations

import datetime as dt
import pathlib

from tests.conftest import NOW, event_to_dict, golden

from meridian.adapters.base import IngestContext
from meridian.adapters.news import NewsRssAdapter

FIXTURE = (pathlib.Path(__file__).parent / "fixtures" / "yahoo_news_AAPL.xml").read_text()
TRADE_DATE = dt.date(2026, 6, 26)


def _ctx():
    universe = (
        {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Information Technology", "index_membership": "SP500"},
        {"symbol": "NVDA", "name": "NVIDIA Corporation", "sector": "Information Technology", "index_membership": "SP500"},
        {"symbol": "AMD", "name": "Advanced Micro Devices", "sector": "Information Technology", "index_membership": "SP500"},
    )
    return IngestContext(trade_date=TRADE_DATE, now=NOW, universe=universe)


def test_parse_feed_tz_and_dedup():
    raws = NewsRssAdapter._parse_feed(FIXTURE, "AAPL", _ctx())
    # 3 items, one duplicate guid -> 2 unique
    assert len(raws) == 2
    first = raws[0]
    assert first.ticker == "AAPL" and first.payload["query_symbol"] == "AAPL"
    et = dt.datetime.fromisoformat(first.payload["published_iso"])
    assert et.tzinfo is not None and et.utcoffset() == dt.timedelta(0)  # tz-aware UTC
    assert et == dt.datetime(2026, 6, 26, 14, 5, tzinfo=dt.timezone.utc)  # pubDate -> event_time


def test_normalize_golden():
    a = NewsRssAdapter()
    ctx = _ctx()
    raws = a._parse_feed(FIXTURE, "AAPL", ctx)
    out = []
    for r in raws:
        out.extend(event_to_dict(e) for e in a.normalize(r, ctx))
    # dual timestamps + family + ticker mapping
    assert all(e["family"] == "news" and e["ticker"] == "AAPL" for e in out)
    assert all(e["ingest_time"] == NOW.isoformat() for e in out)
    # the second headline tags NVDA + AMD as related symbols
    second = next(e for e in out if "rally" in e["payload"]["headline"])
    assert "NVDA" in second["related_symbols"] and "AMD" in second["related_symbols"]
    golden("news_yahoo_normalize", out)


def test_scope_symbol_selection():
    a = NewsRssAdapter({"scope": "movers", "watchlist": ["AAPL", "ZZZZ"]})
    syms = a._symbols(_ctx())
    assert "AAPL" in syms and "ZZZZ" not in syms  # watchlist intersected with universe
    a2 = NewsRssAdapter({"scope": "universe"})
    assert set(a2._symbols(_ctx())) == {"AAPL", "NVDA", "AMD"}
