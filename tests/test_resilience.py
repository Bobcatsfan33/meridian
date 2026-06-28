"""Constraints/DONE: Meridian must never break without Massive.

(1) Massive forced off -> the pipeline runs fully on the free baseline (yfinance + FINRA).
(2) Massive 429/timeout -> the call falls back (empty), the breaker records failure, and the
    pipeline completes with no exception escaping.
Baseline adapters are stubbed offline so the test is deterministic.
"""
from __future__ import annotations

import datetime as dt

import pytest

from meridian.adapters.base import RawEvent
from meridian.config import Config
from meridian.ingest.pipeline import run_ingest

TARGET = dt.date(2026, 6, 26)


@pytest.fixture()
def offline_baseline(monkeypatch):
    from meridian.adapters.finra import FinraAdapter
    from meridian.adapters.yfinance import YFinanceAdapter

    def yf_fetch(self, ctx):
        return [RawEvent("yfinance", ctx.now, "AAPL",
                         {"open": 1, "high": 1, "low": 1, "close": 283.78, "volume": 100,
                          "role": "stock", "trade_date": ctx.trade_date.isoformat()})]

    def finra_fetch(self, ctx):
        self.fetch_failures = 0
        return [RawEvent("finra", ctx.now, "AAPL",
                         {"kind": "short_volume", "as_of": ctx.trade_date.isoformat(),
                          "short_volume": 5.0, "total_volume": 10.0, "short_pct": 0.5})]

    monkeypatch.setattr(YFinanceAdapter, "fetch", yf_fetch)
    monkeypatch.setattr(FinraAdapter, "fetch", finra_fetch)


def test_massive_forced_off_pipeline_runs(tmp_db, offline_baseline):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    # Massive not selected at all -> pure baseline
    res = run_ingest(cfg, TARGET, selected=["yfinance", "finra"])
    assert res.total_normalized >= 2
    assert "price_volume" in res.family_counts and "equity_flow" in res.family_counts
    assert all(s.error is None for s in res.adapter_stats)
    sources = {s.name for s in res.adapter_stats}
    assert "massive" not in sources


def test_massive_429_falls_back_to_baseline(tmp_db, offline_baseline, monkeypatch):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)

    # inject a Massive client whose transport is permanently rate-limited
    from meridian.adapters.massive import MassiveAdapter, MassiveClient

    class Clock:
        def __init__(self): self.t = 0.0
        def __call__(self): return self.t
        def sleep(self, s): self.t += s

    clk = Clock()
    bad = MassiveClient(api_key="k", rate_limit_per_min=600,
                        transport=lambda url, params, timeout: (429, None),
                        clock=clk, sleep=clk.sleep, retries=0, breaker_threshold=1, cache_dir=None)
    monkeypatch.setattr(MassiveAdapter, "_get_client", lambda self, ctx: bad)

    # Massive force-selected alongside baseline; its failures must not break the run
    res = run_ingest(cfg, TARGET, selected=["yfinance", "finra", "massive"])
    assert all(s.error is None for s in res.adapter_stats)          # no exception escaped
    massive = next(s for s in res.adapter_stats if s.name == "massive")
    assert massive.normalized == 0                                  # contributed nothing
    assert bad.breaker.state == "open"                             # breaker tripped on the 429
    # baseline still landed
    assert res.family_counts.get("price_volume", 0) >= 1
    assert res.family_counts.get("equity_flow", 0) >= 1
