"""Step A2: intraday bars land in ticker_state_1m at the correct UTC close timestamp."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from meridian.adapters.yfinance import interval_minutes, intraday_state_rows
from meridian.config import Config
from meridian.ingest.clock import market_close_utc
from meridian.state.intraday import run_intraday
from meridian.storage import connect

ET = ZoneInfo("America/New_York")
TARGET = dt.date(2026, 6, 26)


def _bars():
    # three 5m bars starting 09:30, 09:35, 09:40 ET
    def bar(hh, mm, c):
        return {"start": dt.datetime(2026, 6, 26, hh, mm, tzinfo=ET),
                "open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1000 + mm}
    return [bar(9, 30, 100.0), bar(9, 35, 101.0), bar(9, 40, 100.5)]


def test_interval_minutes():
    assert interval_minutes("5m") == 5 and interval_minutes("1h") == 60 and interval_minutes("x") == 5


def test_intraday_rows_close_ts_utc():
    rows = intraday_state_rows("AAPL", _bars(), 5)
    # first bar starts 09:30 ET -> close 09:35 ET = 13:35 UTC (EDT, UTC-4), stored naive UTC
    assert rows[0][1] == dt.datetime(2026, 6, 26, 13, 35)
    assert rows[1][1] == dt.datetime(2026, 6, 26, 13, 40)
    # ret_1m is bar-over-bar: 2nd bar 101/100 - 1
    assert abs(rows[1][6] - (101.0 / 100.0 - 1.0)) < 1e-9


def test_run_intraday_writes_state_no_lookahead(tmp_db, monkeypatch):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)

    from meridian.adapters.yfinance import YFinanceAdapter
    monkeypatch.setattr(YFinanceAdapter, "download_intraday",
                        lambda self, syms, date, interval: {"AAPL": _bars()})
    # run clock after the bars -> all kept; all are before the 16:00 close
    now = dt.datetime(2026, 6, 26, 20, 0, tzinfo=dt.timezone.utc)
    res = run_intraday(cfg, TARGET, interval="5m", symbols=["AAPL"], now=now)
    assert res.n_rows == 3

    con = connect(tmp_db)
    close_ts = market_close_utc(TARGET).replace(tzinfo=None)
    rows = con.execute("SELECT count(*) FROM ticker_state_1m WHERE ticker='AAPL' AND ts < ?",
                       [close_ts]).fetchone()[0]
    con.close()
    assert rows == 3

    # no-lookahead: a run clock BEFORE the later bars keeps only the elapsed ones
    res2 = run_intraday(cfg, TARGET, interval="5m", symbols=["AAPL"],
                        now=dt.datetime(2026, 6, 26, 13, 36, tzinfo=dt.timezone.utc))
    assert res2.n_rows == 1  # only the 09:30->09:35(13:35 UTC) bar has closed
