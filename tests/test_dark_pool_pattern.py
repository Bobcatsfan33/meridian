"""Step B2: dark_pool_accumulation pattern fires (graded) + renders a card with the
equity-flow drivers, residual, and invalidation."""
from __future__ import annotations

import datetime as dt
import json

from tests.conftest import golden

from meridian.config import Config
from meridian.engine.match import run_match
from meridian.ingest.clock import market_close_utc
from meridian.outputs.build import build_explanations
from meridian.outputs.render import render_card
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


def _scene(con):
    _seed(con, "g_p", "AAPL", "DailyBar", "price_volume", 0.9, {"ret_1m": 0.06})
    _seed(con, "g_s", "AAPL", "ShortVolumeSpike", "equity_flow", 0.95, {"short_pct": 0.6})
    _seed(con, "g_d", "AAPL", "DarkPoolAccumulation", "equity_flow", 0.8, {"off_exchange_share": 9e6})
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [TARGET, "mid_vol_range", ["mid_vol", "range"]])


def test_dark_pool_fires_graded(tmp_db):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    con = connect(tmp_db)
    _scene(con)
    con.close()
    res = run_match(cfg, TARGET, pattern_ids=["dark_pool_accumulation"])
    assert res.per_pattern.get("dark_pool_accumulation") == 1
    assert res.top[0][0] == "AAPL"
    # all legs satisfied (S,P present; concurrent; D present; no news) -> high completeness
    assert res.top[0][2] > 0.8


def test_dark_pool_card_golden(tmp_db):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    con = connect(tmp_db)
    _scene(con)
    con.close()
    run_match(cfg, TARGET, pattern_ids=["dark_pool_accumulation"])
    ev = build_explanations(cfg, TARGET, pattern_id="dark_pool_accumulation")[0]
    assert {d["driver"] for d in ev["drivers"]} >= {"Abnormal short-volume share", "Abnormal price move"}
    assert ev["unexplained_residual"] > 0 and "short-volume" in ev["invalidation"]
    card = render_card(ev)
    assert "dark_pool_accumulation" in card and "Invalidation:" in card
    golden("b2_dark_pool_card", card)
