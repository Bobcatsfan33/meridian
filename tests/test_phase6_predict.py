"""Phase 6: forward odds, reliability/Brier, Granger gate, label no-lookahead, backtest."""
from __future__ import annotations

import datetime as dt
import math

from meridian.config import Config
from meridian.engine.causal import granger_pvalue
from meridian.predict.backtest import backtest
from meridian.predict.calibrate import brier_score, reliability_curve
from meridian.predict.forward import build_profile
from meridian.predict.label import Outcome, label_date_range
from meridian.ingest.clock import market_close_utc
from meridian.storage import connect

TARGET = dt.date(2026, 5, 20)


def test_forward_profile_hit_rate_and_decay():
    outs = [Outcome("f1", "p", "r", "+1d", 0.02, 0.03, -0.01),
            Outcome("f2", "p", "r", "+1d", -0.01, 0.0, -0.02),
            Outcome("f1", "p", "r", "+3d", 0.05, 0.06, -0.01)]
    prof = build_profile(outs, "p", regime_label=None, threshold=0.0)
    h1 = next(h for h in prof.horizons if h.horizon == "+1d")
    assert h1.n == 2 and h1.hit_rate == 0.5
    assert prof.decay[0][0] == "+1d"


def test_reliability_and_brier():
    pairs = [(0.9, True), (0.9, True), (0.1, False), (0.5, True), (0.5, False)]
    bins = reliability_curve(pairs, 5)
    assert all(0 <= b.realized <= 1 for b in bins)
    assert 0 <= brier_score(pairs) <= 1


def test_granger_detects_lead():
    # y[t] = x[t-1] -> x Granger-causes y; reverse should be weaker
    x = [math.sin(i / 3.0) + 0.01 * (i % 5) for i in range(60)]
    y = [0.0] + x[:-1]
    f_xy, p_xy = granger_pvalue(x, y, max_lag=2)
    assert p_xy < 0.05  # x leads y
    f_yx, p_yx = granger_pvalue(y, x, max_lag=2)
    assert math.isnan(p_yx) or p_yx >= p_xy  # reverse not more significant


def test_label_no_lookahead_and_directional(tmp_db):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    con = connect(tmp_db)
    con.execute("INSERT INTO pattern_firings (firing_id,ticker,pattern_id,pattern_ver,window_start,"
                "window_end,completeness,confidence,regime_tags) VALUES (?,?,?,?,?,?,?,?,?)",
                ["f1", "X", "gamma_squeeze", "1", dt.datetime.combine(TARGET, dt.time()),
                 dt.datetime.combine(TARGET, dt.time(23, 59)), 0.7, 0.7, ["mid_vol"]])
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label) VALUES (?,?)",
                [TARGET, "mid_vol_range"])
    con.close()

    # up move on TARGET, continues up -> positive directional forward
    def bar(d, c):
        return {"date": d, "open": c, "high": c, "low": c, "close": c, "volume": 1}
    pw = {"X": [bar(TARGET - dt.timedelta(days=2), 100.0), bar(TARGET - dt.timedelta(days=1), 100.0),
                bar(TARGET, 105.0),  # entry up move
                bar(TARGET + dt.timedelta(days=1), 107.0),
                bar(TARGET + dt.timedelta(days=3), 110.0),
                bar(TARGET + dt.timedelta(days=5), 112.0)]}
    outs = label_date_range(cfg, TARGET, TARGET, price_window=pw)
    h1 = next(o for o in outs if o.horizon == "+1d")
    assert h1.fwd_return > 0  # directional continuation
    assert h1.mfe >= h1.fwd_return >= 0  # MFE bounds


def test_backtest_attaches_residual(tmp_db):
    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(tmp_db)
    con = connect(tmp_db)
    con.execute("INSERT INTO pattern_firings (firing_id,ticker,pattern_id,pattern_ver,window_start,"
                "window_end,completeness,confidence) VALUES (?,?,?,?,?,?,?,?)",
                ["f1", "X", "p", "1", dt.datetime.combine(TARGET, dt.time()),
                 dt.datetime.combine(TARGET, dt.time(23, 59)), 0.7, 0.7])
    con.execute("INSERT INTO historical_pattern_outcomes (firing_id,pattern_id,regime_label,horizon,"
                "fwd_return,mfe,mae) VALUES (?,?,?,?,?,?,?)",
                ["f1", "p", "mid_vol_range", "+3d", 0.03, 0.05, -0.01])
    con.execute("INSERT INTO move_explanations (explanation_id,ticker,unexplained_residual) "
                "VALUES (?,?,?)", ["e1", "X", 0.2])
    con.close()
    res = backtest(cfg, "p", horizon="+3d")
    assert res.n_trades == 1 and res.win_rate == 1.0
    assert abs(res.mean_residual - 0.2) < 1e-9  # honest residual attached
