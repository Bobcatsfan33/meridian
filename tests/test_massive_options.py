"""Step C3: Massive real options -> data_source='massive' (full gamma tier); yfinance live
fallback when Massive unhealthy; fixtures stay test-only (PROXY banner + tier cap)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from meridian.config import Config
from meridian.engine.structural import MatchEvent
from meridian.ingest.clock import market_close_utc
from meridian.options.gex import build_surface
from meridian.options.source import parse_massive_chain
from meridian.outputs.explain import build_evidence
from meridian.outputs.render import render_card

BODY = json.loads((pathlib.Path(__file__).parent / "fixtures" / "massive_options_AAPL.json").read_text())
TARGET = dt.date(2026, 6, 26)
CLOSE = market_close_utc(TARGET).replace(tzinfo=None)
SCORING = Config.load().engine.get("scoring", {})


def test_parse_massive_chain():
    snap = parse_massive_chain(BODY, "AAPL")
    assert snap is not None
    assert snap.data_source == "massive"
    assert snap.spot == 283.78
    assert len(snap.contracts) == 3
    # greeks computable from the parsed IV/strike/expiry
    surf = build_surface(TARGET, snap.spot, snap.contracts)
    assert surf.call_wall is not None


def test_massive_source_selected_when_enabled():
    from meridian.options.source import options_source

    cfg = Config.load()
    cfg.raw.setdefault("adapters", {})["massive"] = {"enabled": True, "options_source": True}
    assert options_source(cfg) == "massive"
    cfg.raw["adapters"]["massive"]["enabled"] = False
    assert options_source(cfg) != "massive"   # falls back to yfinance default


def _gamma_card(data_source: str) -> dict:
    def de(et, abn):
        return MatchEvent(et, CLOSE, "AAPL", "dealer_pos", et, abn, {"data_source": data_source})
    bindings = {"G": de("ShortGamma", 0.9), "K": de("SpotIntoStrike", 0.9),
                "V": de("IVExpansion", 0.9),
                "P": MatchEvent("p", CLOSE, "AAPL", "price_volume", "DailyBar", 0.9, {"ret_1m": 0.05})}
    return build_evidence(
        ticker="AAPL", pattern_id="gamma_squeeze", pattern_ver="1", pattern_desc="d.",
        completeness=0.95, bindings=bindings, move_pct=0.05, abnormal_move_pct=0.04,
        regime_tags=["mid_vol"], sector_etf="XLK", lead_lag_strength=0.0,
        insufficient_history=False, feeds_ok=True, cfg_scoring=SCORING,
        window_start="2026-06-26 00:00:00", window_end="2026-06-26 23:59:59",
        data_source=data_source)


def test_massive_options_full_tier_fixture_capped():
    massive = _gamma_card("massive")
    assert massive["proxy_data"] is False
    assert massive["confidence"]["tier"] in ("High", "Medium")  # eligible, not proxy-capped
    assert "PROXY DATA" not in render_card(massive)

    fixture = _gamma_card("fixture")
    assert fixture["proxy_data"] is True
    assert fixture["confidence"]["tier"] in ("Low", "Unknown")  # capped
    assert "PROXY DATA" in render_card(fixture)
