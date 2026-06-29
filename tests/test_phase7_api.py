"""Phase 7: dashboard API endpoints + scheduler job registration (no live server/network)."""
from __future__ import annotations

import datetime as dt
import json

from fastapi.testclient import TestClient

from meridian.api import create_app
from meridian.config import Config
from meridian.ingest.clock import market_close_utc
from meridian.outputs.explain import build_evidence
from meridian.engine.structural import MatchEvent
from meridian.schedule.scheduler import build_scheduler
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)


def _seed_explanation(tmp_db):
    cfg = Config.load()
    p = MatchEvent("e_p", market_close_utc(TARGET).replace(tzinfo=None), "AAPL", "price_volume",
                   "DailyBar", 0.9, {"ret_1m": 0.07, "rel_volume_pctile": 0.95})
    ev = build_evidence(
        ticker="AAPL", pattern_id="options_led_proxy", pattern_ver="1",
        pattern_desc="Abnormal price + volume, no catalyst.", completeness=0.9,
        bindings={"P": p}, move_pct=0.07, abnormal_move_pct=0.06,
        regime_tags=["mid_vol", "range"], sector_etf="XLK", lead_lag_strength=0.0,
        insufficient_history=False, feeds_ok=True, cfg_scoring=cfg.engine.get("scoring", {}),
        window_start=str(dt.datetime.combine(TARGET, dt.time())),
        window_end=str(dt.datetime.combine(TARGET, dt.time(23, 59, 59))),
    )
    con = connect(tmp_db)
    con.execute(
        "INSERT INTO move_explanations (explanation_id, ticker, window_start, window_end, "
        "unexplained_residual, invalidation, confidence_tier, evidence_object) VALUES (?,?,?,?,?,?,?,?)",
        ["x1", "AAPL", dt.datetime.combine(TARGET, dt.time()),
         dt.datetime.combine(TARGET, dt.time(23, 59, 59)), ev["unexplained_residual"],
         ev["invalidation"], ev["confidence"]["tier"], json.dumps(ev)])
    con.close()


def _client(tmp_db):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    return TestClient(create_app(cfg))


def test_dates_and_scanner(tmp_db):
    _seed_explanation(tmp_db)
    c = _client(tmp_db)
    assert c.get("/api/health").json()["ok"] is True
    assert TARGET.isoformat() in c.get("/api/dates").json()
    data = c.get(f"/api/scanner?date={TARGET}").json()
    rows = data["ranked"]              # scanner now returns {watchlist, ranked}
    assert rows and rows[0]["ticker"] == "AAPL"
    assert "residual" in rows[0] and "tier" in rows[0]
    assert data["watchlist"] == []     # no watchlist configured in this test


def test_card_and_postmortem_endpoints(tmp_db):
    _seed_explanation(tmp_db)
    c = _client(tmp_db)
    card = c.get(f"/api/card/AAPL?date={TARGET}").text
    assert "AAPL" in card and "Not investment advice." in card and "Invalidation:" in card
    pm = c.get(f"/api/postmortem/{TARGET}").text
    assert "EOD POSTMORTEM" in pm
    assert c.get(f"/api/card/ZZZZ?date={TARGET}").status_code == 404


def test_index_serves_dashboard(tmp_db):
    c = _client(tmp_db)
    html = c.get("/").text
    assert "MERIDIAN" in html and "<table" in html


def test_scheduler_registers_both_modes():
    cfg = Config.load()
    sched = build_scheduler(cfg, "both")
    ids = {j.id for j in sched.get_jobs()}
    assert {"premarket", "intraday", "postclose"} <= ids
