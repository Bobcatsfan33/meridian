"""Pure L2 operator + completeness tests (ROADMAP §9)."""
from __future__ import annotations

import datetime as dt

from meridian.engine import structural as st
from meridian.engine.causal import gate_precedence
from meridian.engine.patterns import Leg, Pattern, Role

W = st.MatchWindows(concurrent_window_s=3600, precedes_min_lag_s=0, precedes_max_window_s=7200)


def _ev(eid, minutes, abn=0.5, **payload):
    return st.MatchEvent(eid, dt.datetime(2026, 6, 26, 14, 0) + dt.timedelta(minutes=minutes),
                         "AAPL", "price_volume", "X", abn, payload)


def test_precedes_and_concurrent():
    a, b = _ev("a", 0), _ev("b", 30)
    assert st.precedes(a, b, W)
    assert not st.precedes(b, a, W)
    assert st.concurrent(a, b, W)
    far = _ev("c", 200)
    assert not st.concurrent(a, far, W)
    assert st.independent(a, far, W)


def test_completeness_is_graded_not_boolean():
    pat = Pattern("p", "1", "", (Role("P", "price_volume"),),
                  (Leg("present", role="P"),
                   Leg("feature", role="P", feature="rel_volume_pctile"),
                   Leg("absent", family="news")))
    p = _ev("p", 0, abn=0.8, rel_volume_pctile=0.6)
    r = st.evaluate(pat, {"P": p}, present_families={"price_volume"}, w=W)
    # (0.8 + 0.6 + 1.0) / 3
    assert abs(r.completeness - (0.8 + 0.6 + 1.0) / 3) < 1e-9
    # absence violated -> lower
    r2 = st.evaluate(pat, {"P": p}, present_families={"price_volume", "news"}, w=W)
    assert r2.completeness < r.completeness


def test_precedes_edge_downgraded_until_gated():
    v = gate_precedence("precedes", alpha=0.05)  # untested
    assert v.edge_type == "concurrent" and v.test_pvalue is None
    v2 = gate_precedence("precedes", alpha=0.05, test_stat=4.0, test_pvalue=0.01)
    assert v2.edge_type == "precedes" and v2.tested
    v3 = gate_precedence("concurrent", alpha=0.05)
    assert v3.edge_type == "concurrent"
