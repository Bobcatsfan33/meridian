"""Regime tagging (ROADMAP §12.2). Pure: market state -> label + tags.

Outcomes are always conditioned on regime downstream, so the tagger is deterministic
and golden-tested. Thresholds come from config.featurization (passed in), never here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RegimeThresholds:
    vix_high_pctile: float = 0.70
    vix_low_pctile: float = 0.30
    breadth_broad: float = 0.60
    breadth_weak: float = 0.40


@dataclass(frozen=True)
class Regime:
    vix_level: float
    vix_term: float          # VIX(D)/trailing-mean - 1 (stress proxy)
    index_trend: str         # uptrend | downtrend | range
    breadth: float           # advancers fraction [0,1]
    regime_label: str
    tags: tuple[str, ...]


def vix_bucket(vix_pctile: float, t: RegimeThresholds) -> str:
    if _nan(vix_pctile):
        return "vol_unknown"
    if vix_pctile >= t.vix_high_pctile:
        return "high_vol"
    if vix_pctile <= t.vix_low_pctile:
        return "low_vol"
    return "mid_vol"


def index_trend(close: float, sma: float, sma_prev: float) -> str:
    if _nan(close) or _nan(sma):
        return "range"
    if close > sma and (not _nan(sma_prev) and sma >= sma_prev):
        return "uptrend"
    if close < sma and (not _nan(sma_prev) and sma <= sma_prev):
        return "downtrend"
    return "range"


def breadth_tag(breadth: float, t: RegimeThresholds) -> str:
    if _nan(breadth):
        return "breadth_unknown"
    if breadth >= t.breadth_broad:
        return "broad_advance"
    if breadth <= t.breadth_weak:
        return "broad_decline"
    return "mixed_breadth"


def classify(
    *,
    vix_level: float,
    vix_pctile: float,
    vix_term: float,
    index_close: float,
    index_sma: float,
    index_sma_prev: float,
    breadth: float,
    t: RegimeThresholds,
) -> Regime:
    vb = vix_bucket(vix_pctile, t)
    trend = index_trend(index_close, index_sma, index_sma_prev)
    bt = breadth_tag(breadth, t)
    tags = tuple(x for x in (vb, trend, bt) if not x.endswith("_unknown"))
    label = f"{vb}_{trend}" if not vb.endswith("_unknown") else trend
    return Regime(
        vix_level=vix_level,
        vix_term=vix_term,
        index_trend=trend,
        breadth=breadth,
        regime_label=label,
        tags=tags,
    )


def _nan(x: float) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))
