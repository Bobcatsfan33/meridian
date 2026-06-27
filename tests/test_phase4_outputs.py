"""Phase 4: L3 scoring invariant, constraints, deterministic card render, build integ."""
from __future__ import annotations

import datetime as dt
import json

from tests.conftest import golden

from meridian.config import Config
from meridian.engine import constraints
from meridian.engine.scoring import DriverInput, cap_tier, score
from meridian.engine.structural import MatchEvent
from meridian.ingest.clock import market_close_utc
from meridian.outputs.build import build_explanations
from meridian.outputs.explain import build_evidence
from meridian.outputs.render import render_card
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)
SCORING = Config.load().engine.get("scoring", {})


def test_attribution_plus_residual_is_one():
    r = score(completeness=0.9,
              drivers=[DriverInput("a", 0.9), DriverInput("b", 0.6)],
              corroboration_count=2, lead_lag_strength=0.0, cfg_scoring=SCORING)
    assert abs(sum(d.weight for d in r.drivers) + r.residual - 1.0) < 1e-9
    assert r.residual >= float(SCORING["min_residual"]) - 1e-9  # never rounded to 100%


def test_tier_caps():
    assert cap_tier("High", "Medium") == "Medium"
    assert cap_tier("Low", "Medium") == "Low"


def test_options_proxy_capped_to_medium():
    out = constraints.apply(pattern_id="options_led_proxy", tier="High", cfg_scoring=SCORING,
                            target_abnormality=1.0, sector_abnormality=None,
                            insufficient_history=False, feeds_ok=True)
    assert out.tier == "Medium" and out.notes


def test_insufficient_history_capped_low():
    out = constraints.apply(pattern_id="sector_sympathy", tier="High", cfg_scoring=SCORING,
                            target_abnormality=0.9, sector_abnormality=0.8,
                            insufficient_history=True, feeds_ok=True)
    assert out.tier == "Low"


def test_card_render_is_deterministic_golden():
    p = MatchEvent("e_p", market_close_utc(TARGET).replace(tzinfo=None), "AAPL", "price_volume",
                   "DailyBar", 0.9, {"ret_1m": 0.071, "rel_volume_pctile": 0.95})
    ev = build_evidence(
        ticker="AAPL", pattern_id="options_led_proxy", pattern_ver="1",
        pattern_desc="Abnormal price + volume, no catalyst.", completeness=0.9,
        bindings={"P": p}, move_pct=0.071, abnormal_move_pct=0.06,
        regime_tags=["mid_vol", "range"], sector_etf="XLK", lead_lag_strength=0.0,
        insufficient_history=False, feeds_ok=True, cfg_scoring=SCORING,
        window_start="2026-06-26 00:00:00", window_end="2026-06-26 23:59:59",
    )
    # invariant holds in the evidence object
    assert abs(sum(d["weight"] for d in ev["drivers"]) + ev["unexplained_residual"] - 1.0) < 1e-9
    out = render_card(ev)
    assert "Not investment advice." in out
    assert "Unexplained residual:" in out and "Invalidation:" in out
    golden("phase4_card", out)


# --- integration on a tmp DB ---
def _seed(con, eid, ticker, etype, family, abn, payload):
    ts = market_close_utc(TARGET).replace(tzinfo=None)
    con.execute("INSERT INTO normalized_events (event_id,event_time,ingest_time,ticker,event_type,"
                "family,source,confidence) VALUES (?,?,?,?,?,?,?,?)",
                [eid, ts, ts, ticker, etype, family, "test", 0.9])
    con.execute("INSERT INTO graded_events (event_id,event_time,ticker,event_type,abnormality,"
                "regime_tags,confidence,payload) VALUES (?,?,?,?,?,?,?,?)",
                [eid, ts, ticker, etype, abn, ["mid_vol"], 0.9, json.dumps(payload)])


def test_build_explanations_integration(tmp_db):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    con = connect(tmp_db)
    ts = market_close_utc(TARGET).replace(tzinfo=None)
    _seed(con, "g_aapl", "AAPL", "DailyBar", "price_volume", 0.9, {"ret_1m": 0.07, "rel_volume_pctile": 0.9})
    _seed(con, "g_xlk", "XLK", "ETFBar", "sector_peer", 0.8, {})
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [TARGET, "mid_vol_range", ["mid_vol", "range"]])
    con.execute("INSERT INTO expected_behavior_1m (ticker, ts, abnormal_ret) VALUES (?,?,?)",
                ["AAPL", ts, 0.05])
    # a firing for AAPL (sector_sympathy)
    con.execute("INSERT INTO pattern_firings (firing_id,ticker,pattern_id,pattern_ver,window_start,"
                "window_end,completeness,regime_tags) VALUES (?,?,?,?,?,?,?,?)",
                ["fire_x", "AAPL", "sector_sympathy", "1",
                 dt.datetime.combine(TARGET, dt.time()), dt.datetime.combine(TARGET, dt.time(23, 59, 59)),
                 0.875, ["mid_vol", "range"]])
    con.close()

    evidences = build_explanations(cfg, TARGET)
    assert len(evidences) == 1
    ev = evidences[0]
    assert ev["ticker"] == "AAPL"
    assert ev["unexplained_residual"] > 0
    assert ev["invalidation"]
    assert abs(sum(d["weight"] for d in ev["drivers"]) + ev["unexplained_residual"] - 1.0) < 1e-9

    con = connect(tmp_db)
    stored = con.execute("SELECT unexplained_residual, invalidation, confidence_tier "
                         "FROM move_explanations WHERE ticker='AAPL'").fetchone()
    con.close()
    assert stored[0] > 0 and stored[1] and stored[2]
