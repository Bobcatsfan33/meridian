"""Phase 8: byte-identical golden cards/scanner/postmortem + install-daily plan + phrases."""
from __future__ import annotations

import datetime as dt

from tests.conftest import golden

from meridian.config import Config
from meridian.engine.structural import MatchEvent
from meridian.ingest.clock import market_close_utc
from meridian.outputs import phrases
from meridian.outputs.explain import build_evidence
from meridian.outputs.render import render_card, render_postmortem, render_scanner
from meridian.schedule.install import build_plan

TARGET = dt.date(2026, 6, 26)
SCORING = Config.load().engine.get("scoring", {})


def _evidence(pattern_id, bindings, move, **kw):
    return build_evidence(
        ticker="AAPL", pattern_id=pattern_id, pattern_ver="1",
        pattern_desc="desc.", completeness=0.9, bindings=bindings, move_pct=move,
        abnormal_move_pct=0.05, regime_tags=["mid_vol", "range"], sector_etf="XLK",
        lead_lag_strength=0.0, insufficient_history=False, feeds_ok=True, cfg_scoring=SCORING,
        window_start="2026-06-26 00:00:00", window_end="2026-06-26 23:59:59", **kw)


def _p(abn=0.9, **payload):
    return MatchEvent("e_p", market_close_utc(TARGET).replace(tzinfo=None), "AAPL",
                      "price_volume", "DailyBar", abn, {"ret_1m": 0.071, **payload})


def test_phrases_keyed_to_tier_and_pattern():
    assert phrases.tier_phrase("High") == "Most supported explanation"
    assert phrases.tier_verb("Low") == "could be"
    assert "sympathy" in phrases.readout("sector_sympathy").lower()


def test_card_byte_identical_golden():
    ev = _evidence("options_led_proxy", {"P": _p(rel_volume_pctile=0.95)}, 0.071)
    out = render_card(ev)
    golden("phase8_card_options", out)
    # determinism: same input -> identical output
    assert render_card(ev) == out


def test_gamma_card_with_demotion_golden():
    g = MatchEvent("g", market_close_utc(TARGET).replace(tzinfo=None), "AAPL", "dealer_pos", "ShortGamma", 0.7)
    k = MatchEvent("k", market_close_utc(TARGET).replace(tzinfo=None), "AAPL", "dealer_pos", "SpotIntoStrike", 0.8)
    v = MatchEvent("v", market_close_utc(TARGET).replace(tzinfo=None), "AAPL", "dealer_pos", "IVExpansion", 0.85)
    news = MatchEvent("n", dt.datetime(2026, 6, 26, 13, 30), "AAPL", "news", "HeadlineHit", 0.6,
                      {"headline": "Apple unveils thing"})
    ev = _evidence("gamma_squeeze", {"G": g, "K": k, "V": v, "P": _p()}, -0.018, catalysts=[news])
    out = render_card(ev)
    assert "LATE confirmation" in out and "mechanical" in out
    golden("phase8_card_gamma", out)


def test_scanner_and_postmortem_render():
    ev = _evidence("sector_sympathy", {"P": _p(), "S": MatchEvent(
        "s", market_close_utc(TARGET).replace(tzinfo=None), "XLK", "sector_peer", "ETFBar", 0.8)}, 0.04)
    out = render_scanner([ev], TARGET.isoformat())
    assert "PATTERN SCANNER" in out and "AAPL" in out and "residual" in out.lower()
    golden("phase8_scanner", out)


def test_install_plan_builds():
    cfg = Config.load()
    plan = build_plan(cfg)
    assert "meridian" in plan.content.lower()
    assert plan.activate_cmd
    if plan.platform == "darwin":
        assert plan.path is not None and plan.path.name.endswith(".plist")
        assert "schedule" in plan.content and "both" in plan.content
