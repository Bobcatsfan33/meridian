"""Issue 1: the unexplained residual measures the share of the MOVE (return basis) for
sector_sympathy, not pattern completeness — it must move with abnormal_move_pct and beta.
"""
from __future__ import annotations

import datetime as dt
import json

from meridian.config import Config
from meridian.ingest.clock import market_close_utc
from meridian.outputs.build import build_explanations
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)
CLOSE = market_close_utc(TARGET).replace(tzinfo=None)


def _seed(con, eid, ticker, etype, family, abn, payload):
    con.execute("INSERT INTO normalized_events (event_id,event_time,ingest_time,ticker,event_type,"
                "family,source,confidence) VALUES (?,?,?,?,?,?,?,?)",
                [eid, CLOSE, CLOSE, ticker, etype, family, "test", 0.9])
    con.execute("INSERT INTO graded_events (event_id,event_time,ticker,event_type,abnormality,"
                "regime_tags,confidence,payload) VALUES (?,?,?,?,?,?,?,?)",
                [eid, CLOSE, ticker, etype, abn, ["mid_vol"], 0.9, json.dumps(payload)])


def _build(tmp_db, abnormal_move: float, beta_factor: float) -> dict:
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    con = connect(tmp_db)
    con.execute("DELETE FROM graded_events"); con.execute("DELETE FROM normalized_events")
    con.execute("DELETE FROM pattern_firings"); con.execute("DELETE FROM expected_behavior_1m")
    con.execute("DELETE FROM ticker_state_1m"); con.execute("DELETE FROM regimes_daily")
    _seed(con, "g_aapl", "AAPL", "DailyBar", "price_volume", 0.9, {"ret_1m": abnormal_move})
    _seed(con, "g_xlk", "XLK", "ETFBar", "sector_peer", 0.8, {})
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [TARGET, "mid_vol_range", ["mid_vol", "range"]])
    con.execute("INSERT INTO expected_behavior_1m (ticker, ts, abnormal_ret) VALUES (?,?,?)",
                ["AAPL", CLOSE, abnormal_move])
    # trailing series for beta_to_sector (strictly before close): AAPL = beta_factor * XLK
    for i in range(1, 26):
        ts = CLOSE - dt.timedelta(days=i)
        xr = 0.01 if i % 2 else -0.008
        con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)", ["XLK", ts, xr])
        con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)",
                    ["AAPL", ts, beta_factor * xr])
    # sector move on the target day (XLK ret at close)
    con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)", ["XLK", CLOSE, 0.02])
    # a sector_sympathy firing for AAPL (completeness held constant across scenarios)
    con.execute("INSERT INTO pattern_firings (firing_id,ticker,pattern_id,pattern_ver,window_start,"
                "window_end,completeness,regime_tags) VALUES (?,?,?,?,?,?,?,?)",
                ["fire_aapl", "AAPL", "sector_sympathy", "1", dt.datetime.combine(TARGET, dt.time()),
                 dt.datetime.combine(TARGET, dt.time(23, 59, 59)), 0.875, ["mid_vol", "range"]])
    con.close()
    return next(e for e in build_explanations(cfg, TARGET) if e["ticker"] == "AAPL")


def test_residual_basis_is_return(tmp_db):
    ev = _build(tmp_db, abnormal_move=0.05, beta_factor=1.0)
    assert ev["residual_basis"] == "return"
    # beta=1, sector_move=0.02 -> attributed 0.02 of a 0.05 move -> residual 60%
    assert abs(ev["unexplained_residual"] - 0.6) < 1e-6
    assert abs(sum(d["weight"] for d in ev["drivers"]) + ev["unexplained_residual"] - 1.0) < 1e-6


def test_residual_moves_with_abnormal_move(tmp_db):
    small = _build(tmp_db, abnormal_move=0.05, beta_factor=1.0)["unexplained_residual"]
    big = _build(tmp_db, abnormal_move=0.10, beta_factor=1.0)["unexplained_residual"]
    # same sector contribution, larger unexplained move -> larger residual (NOT completeness-driven)
    assert big > small
    assert abs(big - 0.8) < 1e-6  # (0.10 - 0.02)/0.10


def test_residual_moves_with_beta(tmp_db):
    base = _build(tmp_db, abnormal_move=0.05, beta_factor=1.0)["unexplained_residual"]
    high_beta = _build(tmp_db, abnormal_move=0.05, beta_factor=2.0)["unexplained_residual"]
    # higher beta_to_sector explains more of the move -> smaller residual
    assert high_beta < base
    assert abs(high_beta - 0.2) < 1e-6  # (0.05 - 2*0.02)/0.05


def test_residual_floor_respected(tmp_db):
    # beta*sector explains ~all of the move -> residual clipped up to the min floor
    ev = _build(tmp_db, abnormal_move=0.02, beta_factor=1.0)  # attributed 0.02 == move
    assert ev["residual_basis"] == "return"
    assert ev["unexplained_residual"] >= 0.05 - 1e-9
