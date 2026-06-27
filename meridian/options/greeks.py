"""Black-Scholes greeks computed locally (ROADMAP §4: mibian/custom BS). Pure.

Only gamma (and the d1 helper) is needed for the GEX proxy, but delta is provided for
the dealer-hedging sign. No external dependency — just the standard normal pdf/cdf.
"""
from __future__ import annotations

import math


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def d1(spot: float, strike: float, t_years: float, sigma: float, r: float = 0.0, q: float = 0.0) -> float:
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return float("nan")
    return (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))


def gamma(spot: float, strike: float, t_years: float, sigma: float, r: float = 0.0, q: float = 0.0) -> float:
    """Per-share BS gamma. 0 for degenerate inputs (expired / zero vol)."""
    val = d1(spot, strike, t_years, sigma, r, q)
    if val != val:  # nan
        return 0.0
    return _norm_pdf(val) / (spot * sigma * math.sqrt(t_years))


def delta(spot: float, strike: float, t_years: float, sigma: float, is_call: bool,
          r: float = 0.0, q: float = 0.0) -> float:
    val = d1(spot, strike, t_years, sigma, r, q)
    if val != val:
        return 0.0
    nd = _norm_cdf(val)
    return nd if is_call else nd - 1.0


def year_fraction(as_of, expiry) -> float:
    """ACT/365 between two dates (>= ~1 hour floor so same-day options aren't degenerate)."""
    days = (expiry - as_of).days
    return max(days, 0.04) / 365.0
