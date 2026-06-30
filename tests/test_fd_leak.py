"""Regression: connect-heavy paths must NOT leak file descriptors (the scheduler crash).

Two things are proven:
  - DuckDB connections are released and the process open-fd count does not grow across ~50
    connect/close cycles (DuckDB pools one file handle per DB, so a *leaked* connection is
    hygiene-bad but not itself the fd exhauster).
  - The actual fd exhauster was per-cycle HTTP handles in the intraday loop: the yfinance
    download now reuses ONE requests.Session per run and CLOSES it — verified directly.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

from meridian.config import Config
from meridian.state.intraday import run_intraday
from meridian.storage import connect, db

ET = ZoneInfo("America/New_York")
TARGET = dt.date(2026, 6, 26)


def _nfd() -> int:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        import resource
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]  # fallback (won't assert growth)


def test_db_helper_releases_connections(tmp_db):
    for _ in range(5):                        # warm up caches
        with db(tmp_db) as con:
            con.execute("SELECT 1").fetchone()
    base = _nfd()
    for _ in range(50):
        with db(tmp_db) as con:
            con.execute("SELECT count(*) FROM universe").fetchone()
    assert _nfd() <= base + 8, "fd count grew across 50 db() cycles — connections leaked"


def test_db_helper_closes_on_exception(tmp_db):
    base = _nfd()
    for _ in range(50):
        try:
            with db(tmp_db) as con:
                con.execute("SELECT 1")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
    assert _nfd() <= base + 8, "db() leaked a connection on the exception path"


def test_raw_connect_close_is_stable(tmp_db):
    base = _nfd()
    for _ in range(50):
        con = connect(tmp_db)
        con.execute("SELECT 1").fetchone()
        con.close()
    assert _nfd() <= base + 8


def test_run_intraday_no_fd_growth(tmp_db, monkeypatch):
    """The scheduler's 5-minute hot path: many cycles must not accumulate fds (offline)."""
    from meridian.adapters.yfinance import YFinanceAdapter

    def _bars():
        return [{"start": dt.datetime(2026, 6, 26, 9, 30, tzinfo=ET),
                 "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000}]

    monkeypatch.setattr(YFinanceAdapter, "download_intraday",
                        lambda self, syms, date, interval: {"AAPL": _bars()})
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    now = dt.datetime(2026, 6, 26, 20, 0, tzinfo=dt.timezone.utc)

    for _ in range(3):
        run_intraday(cfg, TARGET, interval="5m", symbols=["AAPL"], now=now)
    base = _nfd()
    for _ in range(40):
        run_intraday(cfg, TARGET, interval="5m", symbols=["AAPL"], now=now)
    assert _nfd() <= base + 8, "run_intraday accumulated fds across 40 cycles"


def test_download_intraday_closes_its_session(monkeypatch):
    """The real fix: the intraday yfinance download reuses ONE session per run and closes
    it (so the long-lived loop doesn't accumulate HTTP handles)."""
    import requests
    import yfinance as yf

    from meridian.adapters.yfinance import YFinanceAdapter

    closes = {"n": 0}

    class TrackedSession(requests.Session):
        def close(self):
            closes["n"] += 1
            super().close()

    monkeypatch.setattr(requests, "Session", TrackedSession)
    monkeypatch.setattr(yf, "download", lambda *a, **k: None)   # no network
    YFinanceAdapter().download_intraday(["AAPL", "MSFT"], TARGET, "5m")
    assert closes["n"] >= 1, "download_intraday did not close its requests.Session"
