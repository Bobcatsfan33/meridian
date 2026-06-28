"""Issue 3: real options source + proxy gating.

- fixture-sourced gamma reads are flagged (proxy_data) and tier-capped, with a PROXY
  banner on the card; live reads are not.
- run_options stamps data_source on gex_surface + dealer_pos events.
- the live yfinance source returns a real chain (network; skipped if unavailable).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from meridian.config import Config
from meridian.engine.structural import MatchEvent
from meridian.ingest.clock import market_close_utc
from meridian.options.gex import ChainContract
from meridian.options.source import load_chain, options_source
from meridian.outputs.explain import build_evidence
from meridian.outputs.render import render_card
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)
CLOSE = market_close_utc(TARGET).replace(tzinfo=None)
SCORING = Config.load().engine.get("scoring", {})


def _gamma_evidence(data_source: str):
    def de(et, abn):
        return MatchEvent(et, CLOSE, "AAPL", "dealer_pos", et, abn, {"data_source": data_source})
    bindings = {
        "G": de("ShortGamma", 0.9), "K": de("SpotIntoStrike", 0.9), "V": de("IVExpansion", 0.9),
        "P": MatchEvent("p", CLOSE, "AAPL", "price_volume", "DailyBar", 0.9, {"ret_1m": 0.05}),
    }
    return build_evidence(
        ticker="AAPL", pattern_id="gamma_squeeze", pattern_ver="1", pattern_desc="d.",
        completeness=0.95, bindings=bindings, move_pct=0.05, abnormal_move_pct=0.04,
        regime_tags=["mid_vol"], sector_etf="XLK", lead_lag_strength=0.0,
        insufficient_history=False, feeds_ok=True, cfg_scoring=SCORING,
        window_start="2026-06-26 00:00:00", window_end="2026-06-26 23:59:59",
        data_source=data_source)


def test_fixture_gamma_is_flagged_and_capped():
    ev = _gamma_evidence("fixture")
    assert ev["proxy_data"] is True and ev["data_source"] == "fixture"
    assert ev["confidence"]["tier"] in ("Low", "Unknown")  # capped, not tradeable
    card = render_card(ev)
    assert "PROXY DATA" in card and "not tradeable" in card.lower()


def test_live_gamma_not_flagged():
    ev = _gamma_evidence("live")
    assert ev["proxy_data"] is False and ev["data_source"] == "live"
    assert "PROXY DATA" not in render_card(ev)


def test_default_source_is_yfinance():
    assert options_source(Config.load()) == "yfinance"


def test_run_options_stamps_data_source(tmp_db, tmp_path):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    fx = tmp_path / "opt" / TARGET.isoformat()
    fx.mkdir(parents=True)
    contracts = []
    for k in range(-10, 11):
        strike = round(100 * (1 + k / 100.0), 2)
        contracts.append({"strike": strike, "expiry": "2026-07-17", "type": "call",
                          "open_interest": 12000 if abs(strike - 101.0) < 0.01 else 800, "iv": 0.55})
        contracts.append({"strike": strike, "expiry": "2026-07-17", "type": "put",
                          "open_interest": 9000 if strike < 100 else 1500, "iv": 0.55})
    (fx / "X.json").write_text(json.dumps({"spot": 100.0, "iv_rank": 0.85, "contracts": contracts}))
    cfg.raw.setdefault("adapters", {})["options_source"] = "fixture"
    cfg.raw["adapters"]["options"] = {"fixtures_dir": str(tmp_path / "opt")}

    from meridian.options.ingest import run_options
    run_options(cfg, TARGET, tickers=["X"])
    con = connect(tmp_db)
    surf_src = con.execute("SELECT DISTINCT data_source FROM gex_surface").fetchall()
    ev_src = con.execute("SELECT payload FROM normalized_events WHERE family='dealer_pos' LIMIT 1").fetchone()
    con.close()
    assert surf_src == [("fixture",)]
    assert json.loads(ev_src[0])["data_source"] == "fixture"


@pytest.mark.network
def test_live_yfinance_chain_smoke():
    cfg = Config.load()  # default options_source=yfinance
    snap = load_chain(cfg, TARGET, "AAPL")
    if snap is None or not snap.contracts:
        pytest.skip("yfinance options unavailable (offline / rate-limited)")
    assert snap.data_source == "live"
    assert snap.spot > 0 and len(snap.contracts) > 0
    assert all(isinstance(c, ChainContract) for c in snap.contracts)
