"""Step 3 — the quiet-name contract: a universe name with no firing gets an HONEST
'no supported explanation' card (tier Unknown, residual ~100%, no fabricated pattern),
rendered like any other card. This is the acceptance for e.g. NVDA on a flat day.
"""
from __future__ import annotations

import datetime as dt

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


def _quiet_universe_name(con, ticker="NVDA"):
    # in the universe, real state, but NO move_explanations firing for the date
    con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)", [ticker, CLOSE, 0.001])
    con.execute("INSERT INTO expected_behavior_1m (ticker, ts, abnormal_ret) VALUES (?,?,?)",
                [ticker, CLOSE, 0.0003])
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [TARGET, "low_vol_range", ["low_vol", "range"]])


def test_quiet_name_is_honest_not_fabricated(tmp_db):
    con = connect(tmp_db)
    _quiet_universe_name(con, "NVDA")
    con.close()
    ev = card_for_ticker(_cfg(tmp_db), "NVDA", TARGET)

    # honest: no pattern invented, nothing attributed, residual is ~100%
    assert ev["pattern"]["id"] == "none" and ev["pattern"]["completeness"] == 0.0
    assert ev["drivers"] == []
    assert ev["confidence"]["tier"] == "Unknown"
    assert ev["unexplained_residual"] == 1.0
    assert ev["readout"] == "No supported explanation — moved in line with expectations."
    # still carries the move + the regime it sat in (built from real state, not invented)
    assert ev["move_pct"] == 0.001 and ev["abnormal_move_pct"] == 0.0003
    assert ev["regime_tags"] == ["low_vol", "range"]
    assert ev["not_investment_advice"] is True
    # attribution + residual invariant holds for the empty case too
    assert sum(d["weight"] for d in ev["drivers"]) + ev["unexplained_residual"] == 1.0


def test_quiet_name_renders_in_card_panel(tmp_db):
    con = connect(tmp_db)
    _quiet_universe_name(con, "NVDA")
    con.close()
    out = render_card(card_for_ticker(_cfg(tmp_db), "NVDA", TARGET))
    assert "NVDA" in out
    assert "No supported explanation" in out
    assert "Unexplained residual: 100.0%" in out
    assert "Not investment advice." in out
    assert "gamma" not in out.lower() and "sympathy" not in out.lower()  # no invented pattern


def test_quiet_name_via_api(tmp_db):
    con = connect(tmp_db)
    _quiet_universe_name(con, "NVDA")
    con.close()
    client = TestClient(create_app(_cfg(tmp_db)))
    body = client.get(f"/api/card?ticker=NVDA&date={TARGET}").json()
    assert body["evidence"]["confidence"]["tier"] == "Unknown"
    assert "No supported explanation" in body["rendered"]
