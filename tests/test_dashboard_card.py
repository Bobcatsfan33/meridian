"""Step 1: /api/card resolution — stored card, graceful universe read, not-tracked read."""
from __future__ import annotations

import datetime as dt
import json

from fastapi.testclient import TestClient

from meridian.api import create_app
from meridian.config import Config
from meridian.ingest.clock import market_close_utc
from meridian.outputs.build import card_for_ticker
from meridian.outputs.render import render_card
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)
CLOSE = market_close_utc(TARGET).replace(tzinfo=None)


def _cfg(tmp_db):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    return cfg


def _seed_stored(con, ticker="AAPL"):
    ev = {"ticker": ticker, "pattern": {"id": "options_led_proxy", "description": "x"},
          "confidence": {"tier": "Medium", "value": 0.7}, "unexplained_residual": 0.1}
    con.execute("INSERT INTO move_explanations (explanation_id, ticker, window_start, window_end, "
                "unexplained_residual, invalidation, confidence_tier, evidence_object) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ["e1", ticker, dt.datetime.combine(TARGET, dt.time()),
                 dt.datetime.combine(TARGET, dt.time(23, 59, 59)), 0.1, "inv", "Medium", json.dumps(ev)])


def _seed_state(con, ticker="NVDA"):
    con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)", [ticker, CLOSE, 0.002])
    con.execute("INSERT INTO expected_behavior_1m (ticker, ts, abnormal_ret) VALUES (?,?,?)",
                [ticker, CLOSE, 0.0005])
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [TARGET, "low_vol_range", ["low_vol", "range"]])


def test_branch_a_returns_stored_evidence(tmp_db):
    con = connect(tmp_db)
    _seed_stored(con, "AAPL")
    con.close()
    ev = card_for_ticker(_cfg(tmp_db), "aapl", TARGET)   # case-insensitive
    assert ev["ticker"] == "AAPL" and ev["pattern"]["id"] == "options_led_proxy"
    assert ev["confidence"]["tier"] == "Medium"          # verbatim, not re-scored


def test_branch_b_graceful_universe(tmp_db):
    con = connect(tmp_db)
    _seed_state(con, "NVDA")    # NVDA is in the universe; no firing
    con.close()
    ev = card_for_ticker(_cfg(tmp_db), "NVDA", TARGET)
    assert ev["pattern"]["id"] == "none"
    assert ev["confidence"]["tier"] == "Unknown"
    assert ev["unexplained_residual"] == 1.0             # residual ~100%, nothing attributed
    assert ev["ad_hoc"] is False and ev["data_source"] == "universe"
    assert ev["move_pct"] == 0.002 and ev["abnormal_move_pct"] == 0.0005
    assert "No supported explanation" in ev["readout"]
    # renders like any other card (deterministic Jinja)
    out = render_card(ev)
    assert "NVDA" in out and "Not investment advice." in out and "Unexplained residual: 100.0%" in out


def test_branch_c_not_tracked(tmp_db):
    ev = card_for_ticker(_cfg(tmp_db), "ZZZZ", TARGET)   # not in universe
    assert ev["pattern"]["id"] == "none" and ev["confidence"]["tier"] == "Unknown"
    assert "Not part of the tracked universe" in ev["pattern"]["description"]
    assert render_card(ev)


def test_api_card_endpoint(tmp_db):
    con = connect(tmp_db)
    _seed_state(con, "NVDA")
    con.close()
    client = TestClient(create_app(_cfg(tmp_db)))
    r = client.get(f"/api/card?ticker=NVDA&date={TARGET}")
    assert r.status_code == 200
    body = r.json()
    assert body["evidence"]["ticker"] == "NVDA"
    assert "Not investment advice." in body["rendered"]
    assert client.get(f"/api/card?ticker=&date={TARGET}").status_code == 400
