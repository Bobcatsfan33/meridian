"""Pipeline tests: dedup, DB upsert, idempotency, registry selection — no network.

Adapters are stubbed by monkeypatching `fetch` so the pipeline runs offline against
a tmp DuckDB; only the network boundary is replaced, the rest is exercised for real.
"""
from __future__ import annotations

import datetime as dt

import pytest
from tests.conftest import NOW, TRADE_DATE

from meridian.adapters.base import RawEvent
from meridian.adapters.registry import build_adapters
from meridian.config import Config
from meridian.ingest import run_ingest
from meridian.storage import connect


@pytest.fixture()
def stub_feeds(monkeypatch):
    from meridian.adapters.yfinance import YFinanceAdapter
    from meridian.adapters.fred import FredAdapter

    def yf_fetch(self, ctx):
        return [
            RawEvent("yfinance", ctx.now, "AAPL",
                     {"open": 275.0, "high": 285.95, "low": 274.21, "close": 283.78,
                      "volume": 261693600, "role": "stock", "trade_date": ctx.trade_date.isoformat()}),
            RawEvent("yfinance", ctx.now, "SPY",
                     {"open": 600.0, "high": 605.0, "low": 599.0, "close": 604.0,
                      "volume": 70000000, "role": "index", "trade_date": ctx.trade_date.isoformat()}),
        ]

    def fred_fetch(self, ctx):
        return [
            RawEvent("fred", ctx.now, None,
                     {"series_id": "DGS10", "description": "10Y", "value": 4.41,
                      "observation_date": ctx.trade_date.isoformat()}),
        ]

    monkeypatch.setattr(YFinanceAdapter, "fetch", yf_fetch)
    monkeypatch.setattr(FredAdapter, "fetch", fred_fetch)


def test_registry_ships_disabled():
    assert build_adapters({}) == []
    assert build_adapters({"yfinance": {"enabled": False}}) == []


def test_registry_selection_overrides_enabled():
    names = [a.name for a in build_adapters({}, selected=["yfinance", "fred"])]
    assert names == ["fred", "yfinance"]  # sorted by (priority, name)


def test_registry_unknown_adapter_raises():
    with pytest.raises(ValueError):
        build_adapters({}, selected=["nope"])


def test_pipeline_writes_and_reports(tmp_db, stub_feeds, monkeypatch):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)  # absolute -> overrides default

    res = run_ingest(cfg, TRADE_DATE, selected=["yfinance", "fred"], now=NOW)

    assert res.total_normalized == 3
    assert res.family_counts == {"macro": 1, "price_volume": 1, "sector_peer": 1}
    assert res.lookahead_violations == 0

    con = connect(tmp_db)
    n_norm = con.execute("SELECT count(*) FROM normalized_events").fetchone()[0]
    n_raw = con.execute("SELECT count(*) FROM raw_market_events").fetchone()[0]
    # event_time must never be after ingest_time (no row received before it happened)
    bad = con.execute(
        "SELECT count(*) FROM normalized_events WHERE ingest_time < event_time"
    ).fetchone()[0]
    con.close()
    assert n_norm == 3
    assert n_raw == 3
    assert bad == 0


def test_pipeline_idempotent(tmp_db, stub_feeds, monkeypatch):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)  # absolute -> overrides default

    run_ingest(cfg, TRADE_DATE, selected=["yfinance", "fred"], now=NOW)
    run_ingest(cfg, TRADE_DATE, selected=["yfinance", "fred"],
               now=NOW + dt.timedelta(hours=2))  # re-run, later clock

    con = connect(tmp_db)
    n = con.execute("SELECT count(*) FROM normalized_events").fetchone()[0]
    con.close()
    assert n == 3  # stable event_id -> upsert, no duplicates
