"""Phase 3 match integration on a tmp DB (real universe, seeded graded events)."""
from __future__ import annotations

import datetime as dt
import json

from meridian.config import Config
from meridian.engine.match import run_match
from meridian.ingest.clock import market_close_utc
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)


def _seed(con, eid, ticker, etype, family, abn, payload):
    ts = market_close_utc(TARGET).replace(tzinfo=None)
    con.execute(
        "INSERT INTO normalized_events (event_id,event_time,ingest_time,ticker,event_type,family,"
        "source,confidence) VALUES (?,?,?,?,?,?,?,?)",
        [eid, ts, ts, ticker, etype, family, "test", 0.9],
    )
    con.execute(
        "INSERT INTO graded_events (event_id,event_time,ticker,event_type,abnormality,regime_tags,"
        "confidence,payload) VALUES (?,?,?,?,?,?,?,?)",
        [eid, ts, ticker, etype, abn, ["mid_vol", "range"], 0.9, json.dumps(payload)],
    )


def _seed_scene(con):
    # AAPL = Information Technology; XLK is its sector ETF (from index_etfs.csv)
    _seed(con, "g_aapl", "AAPL", "DailyBar", "price_volume", 0.9, {"rel_volume_pctile": 0.95})
    _seed(con, "g_xlk", "XLK", "ETFBar", "sector_peer", 0.8, {})
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [TARGET, "mid_vol_range", ["mid_vol", "range"]])


def test_match_fires_with_graded_completeness(tmp_db):
    cfg = Config.load()
    con = connect(tmp_db)
    _seed_scene(con)
    con.close()

    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    res = run_match(cfg, TARGET)

    assert res.n_firings >= 2
    assert "options_led_proxy" in res.per_pattern
    assert "sector_sympathy" in res.per_pattern
    # price_before_news must NOT fire (no news present)
    assert "price_before_news" not in res.per_pattern

    con = connect(tmp_db)
    fires = {r[0]: r[1] for r in con.execute(
        "SELECT pattern_id, round(completeness,4) FROM pattern_firings WHERE ticker='AAPL'").fetchall()}
    # all edges concurrent (causal gate untested) and carry rule_id
    et = con.execute("SELECT DISTINCT edge_type FROM event_edges").fetchall()
    no_rule = con.execute("SELECT count(*) FROM event_edges WHERE rule_id IS NULL").fetchone()[0]
    con.close()
    assert fires["options_led_proxy"] == round((0.9 + 0.95 + 1 + 1) / 4, 4)
    assert fires["sector_sympathy"] == round((0.9 + 0.8 + 0.8 + 1) / 4, 4)
    assert et == [("concurrent",)]
    assert no_rule == 0


def test_match_idempotent(tmp_db):
    cfg = Config.load()
    con = connect(tmp_db)
    _seed_scene(con)
    con.close()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)

    run_match(cfg, TARGET)
    run_match(cfg, TARGET)
    con = connect(tmp_db)
    n = con.execute("SELECT count(*) FROM pattern_firings").fetchone()[0]
    con.close()
    a = run_match(cfg, TARGET)
    assert n == a.n_firings  # no duplication across re-runs
