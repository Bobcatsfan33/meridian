"""Step 5: watchlist names are pinned on top, always carded (graceful if they didn't fire),
and excluded from the ranked list."""
from __future__ import annotations

import datetime as dt
import json

from fastapi.testclient import TestClient

from meridian.api import create_app
from meridian.config import Config
from meridian.ingest.clock import market_close_utc
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)
CLOSE = market_close_utc(TARGET).replace(tzinfo=None)


def _cfg(tmp_db, watchlist):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    cfg.raw["watchlist"] = watchlist
    return cfg


def _seed(con):
    # AAPL fired (stored card); MSFT also fired; NVDA quiet (state only, no firing)
    for tk, tier, conf in [("AAPL", "High", 0.85), ("MSFT", "Medium", 0.6)]:
        ev = {"ticker": tk, "pattern": {"id": "options_led_proxy", "description": "x"},
              "confidence": {"tier": tier, "value": conf}, "unexplained_residual": 0.1,
              "move_pct": 0.03, "timeline": []}
        con.execute("INSERT INTO move_explanations (explanation_id,ticker,window_start,window_end,"
                    "unexplained_residual,invalidation,confidence_tier,evidence_object) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    [f"e_{tk}", tk, dt.datetime.combine(TARGET, dt.time()),
                     dt.datetime.combine(TARGET, dt.time(23, 59, 59)), 0.1, "inv", tier, json.dumps(ev)])
    con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)", ["NVDA", CLOSE, 0.001])
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [TARGET, "low_vol_range", ["low_vol"]])


def test_watchlist_pinned_and_excluded_from_ranked(tmp_db):
    con = connect(tmp_db)
    _seed(con)
    con.close()
    c = TestClient(create_app(_cfg(tmp_db, ["NVDA", "AAPL"])))
    data = c.get(f"/api/scanner?date={TARGET}").json()

    wl = {r["ticker"]: r for r in data["watchlist"]}
    assert set(wl) == {"NVDA", "AAPL"}                 # both pinned, in config order
    assert [r["ticker"] for r in data["watchlist"]] == ["NVDA", "AAPL"]
    assert wl["AAPL"]["tier"] == "High"                # fired -> its stored card
    assert wl["NVDA"]["tier"] == "Unknown" and wl["NVDA"]["pattern"] == "none"  # quiet -> graceful

    ranked = {r["ticker"] for r in data["ranked"]}
    assert "AAPL" not in ranked and "NVDA" not in ranked   # pinned excluded from ranked
    assert "MSFT" in ranked                                  # non-watchlist firing still ranked


def test_empty_watchlist(tmp_db):
    con = connect(tmp_db)
    _seed(con)
    con.close()
    c = TestClient(create_app(_cfg(tmp_db, [])))
    data = c.get(f"/api/scanner?date={TARGET}").json()
    assert data["watchlist"] == []
    assert {r["ticker"] for r in data["ranked"]} == {"AAPL", "MSFT"}
