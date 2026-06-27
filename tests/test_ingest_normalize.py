"""Golden-file tests for adapter normalization (pure, network-free).

Each adapter's normalize() must be deterministic given fixed inputs. Pin the
canonical event output to tests/golden/*.json. Regenerate with REGEN_GOLDEN=1.
"""
from __future__ import annotations

from tests.conftest import NOW, golden, event_to_dict

from meridian.adapters.base import RawEvent
from meridian.adapters.yfinance import YFinanceAdapter
from meridian.adapters.fred import FredAdapter
from meridian.adapters.edgar import EdgarAdapter
from meridian.adapters.news import NewsRssAdapter
from meridian.adapters.earnings import EarningsAdapter


def _norm(adapter, raws, ctx):
    out = []
    for r in raws:
        out.extend(event_to_dict(e) for e in adapter.normalize(r, ctx))
    return out


def test_yfinance_normalize_golden(sample_ctx):
    a = YFinanceAdapter()
    raws = [
        RawEvent("yfinance", NOW, "AAPL",
                 {"open": 275.0, "high": 285.95, "low": 274.21, "close": 283.78,
                  "volume": 261693600, "role": "stock", "trade_date": "2026-06-26"}),
        RawEvent("yfinance", NOW, "XLK",
                 {"open": 250.0, "high": 252.0, "low": 249.0, "close": 251.5,
                  "volume": 8000000, "role": "sector", "trade_date": "2026-06-26"}),
        RawEvent("yfinance", NOW, "^VIX",
                 {"open": 16.0, "high": 17.0, "low": 15.5, "close": 16.4,
                  "volume": 0, "role": "macro", "trade_date": "2026-06-26"}),
    ]
    golden("yfinance_normalize", _norm(a, raws, sample_ctx))


def test_fred_normalize_golden(sample_ctx):
    a = FredAdapter()
    raws = [
        RawEvent("fred", NOW, None,
                 {"series_id": "DGS10", "description": "10Y Treasury yield",
                  "value": 4.41, "observation_date": "2026-06-26"}),
    ]
    golden("fred_normalize", _norm(a, raws, sample_ctx))


def test_edgar_normalize_golden(sample_ctx):
    a = EdgarAdapter()
    raws = [
        RawEvent("sec_edgar", NOW, "AMD",
                 {"form_type": "8-K", "event_type": "Filing8K", "company": "ADVANCED MICRO DEVICES INC",
                  "cik": 2488, "date_filed": "2026-06-26",
                  "url": "https://www.sec.gov/Archives/edgar/data/2488/000000248826000050.txt"}),
    ]
    golden("edgar_normalize", _norm(a, raws, sample_ctx))


def test_news_normalize_golden(sample_ctx):
    a = NewsRssAdapter()
    raws = [
        RawEvent("news_rss", NOW, None,
                 {"title": "NVDA and Apple rally as AMD chips gain share",
                  "link": "https://example.com/a", "feed": "wsj",
                  "published_iso": "2026-06-26T14:05:00+00:00"}),
        RawEvent("news_rss", NOW, None,
                 {"title": "Fed holds rates steady amid soft inflation",
                  "link": "https://example.com/b", "feed": "cnbc",
                  "published_iso": "2026-06-26T18:30:00+00:00"}),
    ]
    golden("news_normalize", _norm(a, raws, sample_ctx))


def test_earnings_normalize_golden(sample_ctx):
    a = EarningsAdapter()
    raws = [
        RawEvent("earnings_yf", NOW, "AAPL",
                 {"date": "2026-06-26", "event_time_iso": "2026-06-26T20:05:00+00:00",
                  "eps_estimate": 1.89, "reported_eps": 2.01, "surprise_pct": 6.3}),
        RawEvent("earnings_yf", NOW, "NVDA",
                 {"date": "2026-06-26", "event_time_iso": None,
                  "eps_estimate": 0.95, "reported_eps": None, "surprise_pct": None}),
    ]
    golden("earnings_normalize", _norm(a, raws, sample_ctx))
