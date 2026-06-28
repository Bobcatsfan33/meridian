"""Provenance guarantee (review checklist B): every normalized_events row records a
data_source in the clean vocabulary; news_events carries ingest_time."""
from __future__ import annotations

import datetime as dt

from meridian.adapters.base import RawEvent
from meridian.config import Config
from meridian.ingest.pipeline import run_ingest
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)
VOCAB = {"massive", "yfinance", "finra", "edgar", "fred", "news_rss", "fixture"}


def test_every_row_has_valid_data_source(tmp_db, monkeypatch):
    from meridian.adapters.finra import FinraAdapter
    from meridian.adapters.yfinance import YFinanceAdapter

    def yf_fetch(self, ctx):
        return [RawEvent("yfinance", ctx.now, "AAPL",
                         {"open": 1, "high": 1, "low": 1, "close": 2, "volume": 9,
                          "role": "stock", "trade_date": ctx.trade_date.isoformat()})]

    def finra_fetch(self, ctx):
        self.fetch_failures = 0
        return [RawEvent("finra", ctx.now, "AAPL",
                         {"kind": "short_volume", "as_of": ctx.trade_date.isoformat(),
                          "short_volume": 5.0, "total_volume": 10.0, "short_pct": 0.5})]

    monkeypatch.setattr(YFinanceAdapter, "fetch", yf_fetch)
    monkeypatch.setattr(FinraAdapter, "fetch", finra_fetch)

    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    run_ingest(cfg, TARGET, selected=["yfinance", "finra"])

    con = connect(tmp_db)
    holes = con.execute("SELECT count(*) FROM normalized_events WHERE data_source IS NULL "
                        "OR data_source=''").fetchone()[0]
    vals = {r[0] for r in con.execute("SELECT DISTINCT data_source FROM normalized_events").fetchall()}
    # provenance mapping: yfinance bar -> yfinance, finra short -> finra
    yf_ds = con.execute("SELECT data_source FROM normalized_events WHERE family='price_volume'").fetchone()[0]
    fin_ds = con.execute("SELECT data_source FROM normalized_events WHERE family='equity_flow'").fetchone()[0]
    con.close()
    assert holes == 0
    assert vals <= VOCAB and vals  # no surprise vocabulary
    assert yf_ds == "yfinance" and fin_ds == "finra"


def test_news_events_has_ingest_time(tmp_db, monkeypatch):
    from meridian.adapters.news import NewsRssAdapter

    def news_fetch(self, ctx):
        self.fetch_failures = 0
        return [RawEvent("yahoo_rss", ctx.now, "AAPL",
                         {"title": "Apple ships chips", "link": "x", "guid": "g1",
                          "published_iso": "2026-06-26T14:00:00+00:00", "query_symbol": "AAPL"})]

    monkeypatch.setattr(NewsRssAdapter, "fetch", news_fetch)
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    run_ingest(cfg, TARGET, selected=["news_rss"])
    con = connect(tmp_db)
    row = con.execute("SELECT event_time, ingest_time FROM news_events LIMIT 1").fetchone()
    ds = con.execute("SELECT data_source FROM normalized_events WHERE family='news' LIMIT 1").fetchone()[0]
    con.close()
    assert row[0] is not None and row[1] is not None and row[1] >= row[0]  # publish <= ingest
    assert ds == "news_rss"
