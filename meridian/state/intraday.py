"""Intraday bar ingestion (Step A2 / Phase 7 intraday loop).

Fetches intraday OHLCV bars (default 5m) and writes them into ticker_state_1m at
intraday granularity (ts = bar CLOSE, UTC). Daily ingestion is unchanged. No-lookahead:
only bars whose close is <= the run clock and strictly before the session close are kept
(the exact session-close row belongs to the daily builder). Idempotent per (symbol, day).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from ..adapters.yfinance import YFinanceAdapter, interval_minutes, intraday_state_rows
from ..config import Config
from ..ingest.clock import UTC, market_close_utc
from ..storage import connect


@dataclass
class IntradaySummary:
    target_date: dt.date
    interval: str
    n_symbols: int = 0
    n_rows: int = 0


def run_intraday(cfg: Config, target_date: dt.date, interval: str | None = None,
                 symbols: list[str] | None = None, now: dt.datetime | None = None) -> IntradaySummary:
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)
    interval = interval or cfg.raw.get("adapters", {}).get("yfinance", {}).get("intraday_interval", "5m")
    mins = interval_minutes(interval)
    close_ts = market_close_utc(target_date).replace(tzinfo=None)
    syms = symbols or _default_symbols(cfg)

    adapter = YFinanceAdapter()
    bars_by_sym = adapter.download_intraday(syms, target_date, interval)

    con = connect(cfg.duckdb_path)
    summ = IntradaySummary(target_date=target_date, interval=interval)
    try:
        for sym, bars in bars_by_sym.items():
            rows = [
                r for r in intraday_state_rows(sym, bars, mins)
                if r[1] <= now and r[1] < close_ts  # no-lookahead + leave the close row to daily
            ]
            if not rows:
                continue
            # idempotent: clear this symbol's intraday rows for the day, then insert
            con.execute(
                "DELETE FROM ticker_state_1m WHERE ticker=? AND CAST(ts AS DATE)=? AND ts < ?",
                [sym, target_date, close_ts])
            con.executemany(
                "INSERT INTO ticker_state_1m (ticker, ts, close, vwap, rel_volume, atr, ret_1m) "
                "VALUES (?,?,?,?,?,?,?)",
                [(a, b, c, d, _null(e), g, _null(h)) for (a, b, c, d, e, g, h) in rows])
            summ.n_symbols += 1
            summ.n_rows += len(rows)
        return summ
    finally:
        con.close()


def _default_symbols(cfg: Config) -> list[str]:
    from ..options.source import DEFAULT_LIVE_TICKERS
    wl = (cfg.raw.get("adapters", {}).get("news_rss", {}) or {}).get("watchlist")
    return wl if isinstance(wl, list) and wl else DEFAULT_LIVE_TICKERS


def _null(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
