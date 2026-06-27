"""Causal-edge gate (ROADMAP §12.4). Decides whether an observed `precedes` relation
may be trusted as a causal `precedes` edge, or must be downgraded.

HARD RULE: edge_type is set to `precedes` ONLY if a statistical lead-lag test passes
(p < engine.causal_test_alpha). Phase 3 has no historical sample to test on, so the
gate returns UNTESTED -> the edge is persisted downgraded to `concurrent`. Phase 6
replaces `test_precedence` with a real Granger / transfer-entropy test.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CausalVerdict:
    edge_type: str            # precedes (gated) | concurrent (downgraded)
    test_stat: float | None
    test_pvalue: float | None
    tested: bool


def gate_precedence(observed_relation: str, alpha: float,
                    test_stat: float | None = None,
                    test_pvalue: float | None = None) -> CausalVerdict:
    """Map an observed relation + optional test result to a trusted edge_type.

    `concurrent`/`contradicts` are not precedence claims and pass through unchanged.
    An observed `precedes` becomes a trusted `precedes` edge only when a test result
    is supplied and significant; otherwise it is downgraded to `concurrent`.
    """
    if observed_relation != "precedes":
        return CausalVerdict(observed_relation, test_stat, test_pvalue, tested=False)
    if test_pvalue is not None and test_pvalue < alpha:
        return CausalVerdict("precedes", test_stat, test_pvalue, tested=True)
    # untested or not significant -> downgrade (cannot assert causal precedence)
    return CausalVerdict("concurrent", test_stat, test_pvalue, tested=test_pvalue is not None)


def granger_pvalue(cause: list[float], effect: list[float], max_lag: int = 3) -> tuple[float, float]:
    """Does `cause` Granger-cause `effect`? Returns (best_F_stat, min_pvalue) across lags.
    Uses statsmodels when available; falls back to a lead-lag correlation t-test. NaN if
    the series are too short to test."""
    pairs = [(c, e) for c, e in zip(cause, effect)
             if _ok(c) and _ok(e)]
    n = len(pairs)
    if n < max_lag * 2 + 4:
        return float("nan"), float("nan")
    c = [p[0] for p in pairs]
    e = [p[1] for p in pairs]
    try:
        return _granger_statsmodels(c, e, max_lag)
    except Exception:
        return _lead_lag_ttest(c, e, max_lag)


def _granger_statsmodels(cause: list[float], effect: list[float], max_lag: int) -> tuple[float, float]:
    import numpy as np
    from statsmodels.tsa.stattools import grangercausalitytests

    # column order [effect, cause]: tests whether `cause` (col 1) helps predict effect (col 0)
    import warnings

    data = np.column_stack([np.asarray(effect, float), np.asarray(cause, float)])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            res = grangercausalitytests(data, maxlag=max_lag, verbose=False)
        except TypeError:  # `verbose` removed in newer statsmodels
            res = grangercausalitytests(data, maxlag=max_lag)
    best_f, best_p = float("nan"), float("nan")
    for lag in res:
        f, p = res[lag][0]["ssr_ftest"][0], res[lag][0]["ssr_ftest"][1]
        if best_p != best_p or p < best_p:
            best_f, best_p = f, p
    return float(best_f), float(best_p)


def _lead_lag_ttest(cause: list[float], effect: list[float], max_lag: int) -> tuple[float, float]:
    """Fallback: strongest positive-lag cross-correlation (cause leads effect) -> t-stat -> p."""
    import math

    best_t, best_p = float("nan"), float("nan")
    for lag in range(1, max_lag + 1):
        x = cause[:-lag]
        y = effect[lag:]
        r = _pearson(x, y)
        if r != r:
            continue
        n = len(x)
        if n <= 2 or abs(r) >= 1:
            continue
        t = r * math.sqrt((n - 2) / (1 - r * r))
        from statistics import NormalDist
        p = 2 * (1 - NormalDist().cdf(abs(t)))
        if best_p != best_p or p < best_p:
            best_t, best_p = t, p
    return best_t, best_p


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = sum((a - mx) ** 2 for a in x) ** 0.5
    dy = sum((b - my) ** 2 for b in y) ** 0.5
    return num / (dx * dy) if dx and dy else float("nan")


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and v != v)
