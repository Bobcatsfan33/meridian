"""Phase 5: greeks, GEX proxy, dealer-pos events, options grading, mechanical class,
and the gamma-squeeze pattern firing end to end (no network — synthetic chain)."""
from __future__ import annotations

import datetime as dt
import json


from meridian.config import Config
from meridian.engine import featurize_grade as fg
from meridian.engine.mechanical import classify
from meridian.engine.structural import MatchEvent
from meridian.options.gex import ChainContract, build_surface
from meridian.options import greeks
from meridian.options.events import derive_events
from meridian.options.source import ChainSnapshot

AS_OF = dt.date(2026, 6, 26)
EXPIRY = dt.date(2026, 7, 17)


def _chain(spot: float):
    contracts = []
    wall = round(spot * 1.01, 2)  # call wall just above spot
    for k in range(-10, 11):
        strike = round(spot * (1 + k / 100.0), 2)
        contracts.append(ChainContract(strike, EXPIRY, True,
                                        12000 if abs(strike - wall) < 0.01 else 800, 0.55))
        contracts.append(ChainContract(strike, EXPIRY, False, 9000 if strike < spot else 1500, 0.55))
    return ChainSnapshot("X", spot, 0.85, contracts)


def test_greeks_gamma_positive_and_symmetric():
    g_atm = greeks.gamma(100, 100, 0.1, 0.3)
    g_otm = greeks.gamma(100, 130, 0.1, 0.3)
    assert g_atm > g_otm > 0
    assert greeks.gamma(100, 100, 0, 0.3) == 0.0  # expired -> 0


def test_gex_surface_short_gamma_and_walls():
    snap = _chain(100.0)
    s = build_surface(AS_OF, 100.0, snap.contracts)
    assert s.net_gex < 0          # puts heavy -> dealers net short gamma
    assert -1.0 <= s.net_gex_ratio <= 0.0
    assert s.call_wall is not None and s.put_wall is not None


def test_derive_events_emits_dealer_positioning():
    snap = _chain(100.0)
    s = build_surface(AS_OF, 100.0, snap.contracts)
    types = {e["event_type"] for e in derive_events(snap, s)}
    assert {"ShortGamma", "SpotIntoStrike", "IVExpansion"} <= types


def test_grade_options_short_gamma_high():
    opt = {"spot_into_strike_pct": 0.03, "neutral_iv_rank": 0.5}
    r = fg.grade_options({"event_type": "ShortGamma", "payload": {"net_gex_ratio": -0.9}}, opt)
    assert r.abnormality == 0.9 and r.method == "dealer_positioning"
    r2 = fg.grade_options({"event_type": "SpotIntoStrike", "payload": {"dist_ratio": 0.0}}, opt)
    assert r2.abnormality == 1.0


def test_mechanical_classifier_demotes_headline():
    g = MatchEvent("g", dt.datetime(2026, 6, 26, 20, 0), "JPM", "dealer_pos", "ShortGamma", 0.8)
    news = MatchEvent("n", dt.datetime(2026, 6, 26, 13, 0), "JPM", "news", "HeadlineHit", 0.6)
    mc = classify({"G": g}, [news])
    assert mc.label == "mechanical" and mc.demote_news
    mc2 = classify({"P": None}, [news])
    assert mc2.label == "informational"


def test_options_ingest_and_gamma_squeeze_fires(tmp_db, tmp_path):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    # write a fixture chain into a temp fixtures dir
    fixtures = tmp_path / "opt" / AS_OF.isoformat()
    fixtures.mkdir(parents=True)
    snap = _chain(100.0)
    (fixtures / "X.json").write_text(json.dumps({
        "spot": 100.0, "iv_rank": 0.85,
        "contracts": [{"strike": c.strike, "expiry": c.expiry.isoformat(),
                       "type": "call" if c.is_call else "put",
                       "open_interest": c.open_interest, "iv": c.iv} for c in snap.contracts],
    }))
    cfg.raw.setdefault("adapters", {})["options_source"] = "fixture"
    cfg.raw["adapters"]["options"] = {
        "enabled": True, "fixtures_dir": str(tmp_path / "opt")}

    # seed a price_volume event + ticker_state so P binds and grades
    from meridian.ingest.clock import market_close_utc
    from meridian.storage import connect
    ts = market_close_utc(AS_OF).replace(tzinfo=None)
    con = connect(tmp_db)
    con.execute("INSERT INTO normalized_events (event_id,event_time,ingest_time,ticker,event_type,"
                "family,source,confidence,payload) VALUES (?,?,?,?,?,?,?,?,?)",
                ["p_x", ts, ts, "X", "DailyBar", "price_volume", "test", 0.95, json.dumps({"ret_1m": 0.08})])
    for i in range(40):
        con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)",
                    ["X", ts - dt.timedelta(days=40 - i), 0.001 * (i % 3 - 1)])
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [AS_OF, "mid_vol_range", ["mid_vol", "range"]])
    con.close()

    from meridian.engine.featurize import featurize
    from meridian.engine.match import run_match
    from meridian.options.ingest import run_options

    run_options(cfg, AS_OF, tickers=["X"])
    featurize(connect(tmp_db), cfg, AS_OF)  # grades dealer_pos + price
    res = run_match(cfg, AS_OF, pattern_ids=["gamma_squeeze"])
    assert res.n_firings == 1
    assert res.per_pattern == {"gamma_squeeze": 1}
    assert res.top[0][0] == "X"
    assert 0.0 < res.top[0][2] <= 1.0  # graded completeness
