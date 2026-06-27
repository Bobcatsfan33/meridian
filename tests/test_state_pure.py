"""Pure-function tests for L1 stats and regime tagging (golden where useful)."""
from __future__ import annotations

import math

from meridian.state import baseline as bl
from meridian.state import regime as rg


def test_percentile_rank_basic():
    assert bl.percentile_rank(5, [1, 2, 3, 4, 5]) == 1.0
    assert bl.percentile_rank(0, [1, 2, 3, 4, 5]) == 0.0
    assert bl.percentile_rank(3, [1, 2, 3, 4, 5]) == 0.6


def test_abnormality_from_magnitude_uses_absolute():
    trailing = [0.001, -0.002, 0.0015, -0.001]
    assert bl.abnormality_from_magnitude(0.08, trailing) == 1.0  # huge move -> top
    assert 0.0 <= bl.abnormality_from_magnitude(0.0, trailing) <= 1.0


def test_percentile_rank_empty_is_nan():
    assert math.isnan(bl.percentile_rank(1.0, []))


def test_rolling_returns_and_beta():
    closes = [100, 101, 102, 103]
    rets = bl.rolling_returns(closes)
    assert len(rets) == 3
    # perfectly correlated -> beta 1, alpha ~0
    beta, alpha = bl.beta_alpha(rets, rets)
    assert abs(beta - 1.0) < 1e-9
    assert abs(alpha) < 1e-9


def test_regime_classify_labels():
    t = rg.RegimeThresholds()
    r = rg.classify(
        vix_level=30, vix_pctile=0.9, vix_term=0.1,
        index_close=110, index_sma=100, index_sma_prev=99,
        breadth=0.7, t=t,
    )
    assert r.regime_label == "high_vol_uptrend"
    assert "high_vol" in r.tags and "uptrend" in r.tags and "broad_advance" in r.tags

    r2 = rg.classify(
        vix_level=12, vix_pctile=0.1, vix_term=-0.05,
        index_close=90, index_sma=100, index_sma_prev=101,
        breadth=0.3, t=t,
    )
    assert r2.regime_label == "low_vol_downtrend"
    assert "broad_decline" in r2.tags
