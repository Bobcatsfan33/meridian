"""Pure statistics for Layer-1 grading: trailing distributions -> [0,1] abnormality.

Abnormality is a PERCENTILE RANK of the current magnitude within the name's own
trailing history (ROADMAP §9 L1) — not a global cutoff. All callers pass a trailing
window that excludes the value being graded (no-lookahead). Deterministic; golden-tested.
"""
from __future__ import annotations

import math
from typing import Sequence


def mean(xs: Sequence[float]) -> float:
    xs = [x for x in xs if x is not None and not _isnan(x)]
    return sum(xs) / len(xs) if xs else float("nan")


def stdev(xs: Sequence[float]) -> float:
    xs = [x for x in xs if x is not None and not _isnan(x)]
    if len(xs) < 2:
        return float("nan")
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def zscore(value: float, trailing: Sequence[float]) -> float:
    s = stdev(trailing)
    if _isnan(s) or s == 0:
        return float("nan")
    return (value - mean(trailing)) / s


def percentile_rank(value: float, trailing: Sequence[float]) -> float:
    """Fraction of trailing values <= `value`, in [0,1]. Ties count as <=.

    Empty trailing -> nan (caller falls back to the insufficient-history grade).
    """
    vals = [x for x in trailing if x is not None and not _isnan(x)]
    if not vals:
        return float("nan")
    le = sum(1 for x in vals if x <= value)
    return le / len(vals)


def abnormality_from_magnitude(value: float, trailing: Sequence[float]) -> float:
    """Grade |value| against the trailing distribution of |trailing| -> [0,1].

    A move at the 97th percentile of this name's own history grades ~0.97; the same
    notional in a quiet name grades low. This is the core L1 contract.
    """
    mags = [abs(x) for x in trailing if x is not None and not _isnan(x)]
    p = percentile_rank(abs(value), mags)
    return p


def rolling_returns(closes: Sequence[float]) -> list[float]:
    """Simple daily returns from a close series (len-1 outputs)."""
    out: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev and not _isnan(prev) and cur is not None and not _isnan(cur):
            out.append(cur / prev - 1.0)
        else:
            out.append(float("nan"))
    return out


def beta_alpha(stock_rets: Sequence[float], mkt_rets: Sequence[float]) -> tuple[float, float]:
    """OLS beta & alpha of stock on market returns (paired, NaNs dropped)."""
    pairs = [
        (s, m)
        for s, m in zip(stock_rets, mkt_rets)
        if s is not None and m is not None and not _isnan(s) and not _isnan(m)
    ]
    if len(pairs) < 2:
        return float("nan"), float("nan")
    sm = [p[0] for p in pairs]
    mk = [p[1] for p in pairs]
    mbar, sbar = mean(mk), mean(sm)
    var = sum((m - mbar) ** 2 for m in mk)
    if var == 0:
        return float("nan"), float("nan")
    cov = sum((m - mbar) * (s - sbar) for s, m in pairs)
    beta = cov / var
    alpha = sbar - beta * mbar
    return beta, alpha


def _isnan(x: float) -> bool:
    return isinstance(x, float) and math.isnan(x)
