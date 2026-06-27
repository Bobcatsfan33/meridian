"""Phase 2 build_state + L1 featurize on a synthetic window (no network).

Golden-pins grading output and asserts the no-lookahead invariant: adding bars dated
AFTER the target date must not change the target day's grades.
"""
from __future__ import annotations

import datetime as dt
import json

from tests.conftest import golden

from meridian.config import Config
from meridian.engine.featurize import featurize
from meridian.ingest.clock import market_close_utc
from meridian.state import build_state
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)


def _bars(symbol: str, n: int = 40, last_jump: float | None = None) -> list[dict]:
    """Deterministic ascending daily bars ending on TARGET."""
    base = {"SPY": 600.0, "AAPL": 100.0, "AMD": 50.0, "XLK": 200.0, "^VIX": 18.0}[symbol]
    slope = {"SPY": 0.5, "AAPL": 0.05, "AMD": 0.01, "XLK": 0.3, "^VIX": 0.0}[symbol]
    bars = []
    for i in range(n):
        d = TARGET - dt.timedelta(days=(n - 1 - i))
        c = base + slope * i + (0.1 if symbol == "^VIX" and i % 2 else 0.0)
        if i == n - 1 and last_jump is not None:
            c = c * (1 + last_jump)
        bars.append({"date": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1_000_000 + i})
    return bars


def _window(extra_future: bool = False) -> dict:
    w = {
        "SPY": _bars("SPY"),
        "AAPL": _bars("AAPL", last_jump=0.08),  # big idiosyncratic move on target
        "AMD": _bars("AMD"),
        "XLK": _bars("XLK"),
        "^VIX": _bars("^VIX"),
    }
    if extra_future:
        for sym, bars in w.items():
            last = bars[-1]
            fut = dict(last)
            fut["date"] = TARGET + dt.timedelta(days=3)
            fut["close"] = last["close"] * 1.5  # wild future bar must be ignored
            bars.append(fut)
    return w


_META = {
    "AAPL": {"kind": "stock", "role": "stock", "sector": "Information Technology"},
    "AMD": {"kind": "stock", "role": "stock", "sector": "Information Technology"},
    "SPY": {"kind": "etf", "role": "index", "sector_name": "S&P 500", "sector": None},
    "XLK": {"kind": "etf", "role": "sector", "sector_name": "Information Technology", "sector": None},
    "^VIX": {"kind": "macro", "role": "macro", "sector": None},
}


def _seed_events(con):
    close = market_close_utc(TARGET).replace(tzinfo=None)
    rows = [
        ("e_aapl", close, "AAPL", "DailyBar", "price_volume", 0.95),
        ("e_amd", close, "AMD", "DailyBar", "price_volume", 0.95),
        ("e_xlk", close, "XLK", "ETFBar", "sector_peer", 0.95),
        ("e_vix", close, "^VIX", "MacroQuote", "macro", 0.95),
        ("e_8k", close, "AAPL", "Filing8K", "filing", 0.99),
        ("e_news", close, "AAPL", "HeadlineHit", "news", 0.80),
    ]
    con.executemany(
        "INSERT INTO normalized_events (event_id,event_time,ingest_time,ticker,event_type,family,"
        "source,confidence) VALUES (?,?,?,?,?,?,?,?)",
        [(a, b, b, c, d, e, "test", f) for (a, b, c, d, e, f) in rows],
    )


def _grade_map(con):
    rows = con.execute(
        "SELECT event_id, event_type, ticker, round(abnormality,4), payload FROM graded_events "
        "ORDER BY event_id"
    ).fetchall()
    out = {}
    for eid, et, tk, abn, payload in rows:
        p = json.loads(payload)
        out[eid] = {"event_type": et, "ticker": tk, "abnormality": abn,
                    "method": p.get("grade_method")}
    return out


def _run(tmp_db, extra_future=False):
    cfg = Config.load()
    con = connect(tmp_db)
    _seed_events(con)
    build_state(con, cfg, TARGET, _window(extra_future), _META)
    featurize(con, cfg, TARGET)
    gm = _grade_map(con)
    con.close()
    return gm


def test_featurize_grades_all_events_golden(tmp_db):
    gm = _run(tmp_db)
    assert len(gm) == 6
    # AAPL's +8% idiosyncratic move tops its own-regime distribution
    assert gm["e_aapl"]["abnormality"] == 1.0
    assert gm["e_aapl"]["method"] == "own_regime_percentile"
    # filing/news graded by family prior (no trailing history)
    assert gm["e_8k"]["abnormality"] == 0.70
    assert gm["e_news"]["abnormality"] == 0.60
    golden("phase2_grades", gm)


def test_no_lookahead_future_bars_ignored(tmp_db):
    base = _run(tmp_db)
    # fresh DB run WITH wild future bars appended must produce identical target grades
    cfg = Config.load()
    con = connect(tmp_db)
    con.execute("DELETE FROM graded_events")
    con.execute("DELETE FROM ticker_state_1m")
    build_state(con, cfg, TARGET, _window(extra_future=True), _META)
    featurize(con, cfg, TARGET)
    after = _grade_map(con)
    con.close()
    assert base == after, "future bars (event_time > t) must not affect target-day grades"


def test_expected_behavior_populated(tmp_db):
    cfg = Config.load()
    con = connect(tmp_db)
    _seed_events(con)
    build_state(con, cfg, TARGET, _window(), _META)
    close = market_close_utc(TARGET).replace(tzinfo=None)
    n = con.execute("SELECT count(*) FROM expected_behavior_1m WHERE ts=?", [close]).fetchone()[0]
    # residual denominator computable: abnormal_ret present for stocks
    abn = con.execute("SELECT abnormal_ret FROM expected_behavior_1m WHERE ticker='AAPL' AND ts=?",
                      [close]).fetchone()
    regime = con.execute("SELECT regime_label, regime_tags FROM regimes_daily WHERE trade_date=?",
                         [TARGET]).fetchone()
    con.close()
    assert n == 2  # AAPL, AMD
    assert abn is not None and abn[0] is not None
    assert regime[0] and regime[1]
