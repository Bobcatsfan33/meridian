"""Layer-3 scoring (ROADMAP §9): transparent weighted confidence + driver attribution.

Confidence is a renormalized weighted blend of completeness, abnormality, corroboration,
lead-lag strength, and historical hit-rate (neutral until Phase 6). Attribution splits
the *explained* fraction across drivers; HARD RULE: sum(driver weights) + residual = 1.0,
and the explained fraction is capped so the residual is never rounded to zero. Each
weight expands to its four inputs (§9). Pure & golden-tested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DriverInput:
    name: str
    contribution: float  # abnormality or L1 feature value driving this leg


@dataclass(frozen=True)
class Driver:
    driver: str
    weight: float
    inputs: dict[str, Any]


@dataclass
class ScoreResult:
    confidence: float
    tier: str
    explained: float
    residual: float
    drivers: list[Driver] = field(default_factory=list)


def score(
    *,
    completeness: float,
    drivers: list[DriverInput],
    corroboration_count: int,
    lead_lag_strength: float,
    cfg_scoring: dict,
) -> ScoreResult:
    w = cfg_scoring.get("weights", {})
    hit_rate = float(cfg_scoring.get("neutral_hit_rate", 0.5))
    min_resid = float(cfg_scoring.get("min_residual", 0.05))

    abn = _mean([d.contribution for d in drivers]) if drivers else 0.0
    corro = min(1.0, corroboration_count / 4.0)
    features = {
        "completeness": completeness,
        "abnormality": abn,
        "corroboration": corro,
        "lead_lag": lead_lag_strength,
        "hit_rate": hit_rate,
    }
    wsum = sum(float(w.get(k, 0.0)) for k in features) or 1.0
    confidence = sum(float(w.get(k, 0.0)) * v for k, v in features.items()) / wsum

    explained = min(completeness, 1.0 - min_resid)
    total_contrib = sum(max(0.0, d.contribution) for d in drivers)
    out_drivers: list[Driver] = []
    if total_contrib > 0:
        for d in drivers:
            share = max(0.0, d.contribution) / total_contrib
            out_drivers.append(Driver(
                driver=d.name,
                weight=round(explained * share, 6),
                inputs={
                    "lead_lag_strength": round(lead_lag_strength, 6),
                    "corroboration_count": corroboration_count,
                    "signal_abnormality": round(d.contribution, 6),
                    "historical_hit_rate": round(hit_rate, 6),
                },
            ))
    residual = round(1.0 - sum(d.weight for d in out_drivers), 6)
    tier = _tier(confidence, cfg_scoring)
    return ScoreResult(confidence=round(confidence, 6), tier=tier,
                       explained=round(sum(d.weight for d in out_drivers), 6),
                       residual=residual, drivers=out_drivers)


def _tier(confidence: float, s: dict) -> str:
    if confidence >= float(s.get("tier_high", 0.70)):
        return "High"
    if confidence >= float(s.get("tier_medium", 0.45)):
        return "Medium"
    if confidence >= float(s.get("tier_low", 0.20)):
        return "Low"
    return "Unknown"


def cap_tier(tier: str, cap: str) -> str:
    order = ["Unknown", "Low", "Medium", "High"]
    if tier not in order or cap not in order:
        return tier
    return tier if order.index(tier) <= order.index(cap) else cap


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else 0.0
