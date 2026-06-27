"""Forward-return distribution per pattern × regime (ROADMAP §12.3).

Produces P(directional return > threshold), the return distribution (mean/median/std),
hit-rate, and a decay profile across horizons. Outputs are conditional odds, never a
guarantee, and always presented alongside the residual downstream. Pure & golden-tested.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HorizonOdds:
    horizon: str
    n: int
    hit_rate: float
    mean_return: float
    median_return: float
    std_return: float
    p_gt_threshold: float
    mean_mfe: float
    mean_mae: float


@dataclass
class ForwardProfile:
    pattern_id: str
    regime_label: str | None
    horizons: list[HorizonOdds] = field(default_factory=list)
    decay: list[tuple[str, float]] = field(default_factory=list)  # (horizon, hit_rate)


def build_profile(outcomes: list, pattern_id: str, regime_label: str | None,
                  threshold: float = 0.0) -> ForwardProfile:
    """outcomes: iterable of objects with .horizon/.fwd_return/.mfe/.mae/.regime_label."""
    rows = [o for o in outcomes if o.pattern_id == pattern_id
            and (regime_label is None or o.regime_label == regime_label)]
    by_h: dict[str, list] = {}
    for o in rows:
        by_h.setdefault(o.horizon, []).append(o)

    horizons: list[HorizonOdds] = []
    for h in sorted(by_h, key=_h_key):
        rets = [o.fwd_return for o in by_h[h]]
        horizons.append(HorizonOdds(
            horizon=h, n=len(rets),
            hit_rate=_frac(rets, lambda r: r > 0),
            mean_return=statistics.fmean(rets),
            median_return=statistics.median(rets),
            std_return=statistics.pstdev(rets) if len(rets) > 1 else 0.0,
            p_gt_threshold=_frac(rets, lambda r: r > threshold),
            mean_mfe=statistics.fmean([o.mfe for o in by_h[h]]),
            mean_mae=statistics.fmean([o.mae for o in by_h[h]]),
        ))
    decay = [(ho.horizon, ho.hit_rate) for ho in horizons]
    return ForwardProfile(pattern_id, regime_label, horizons, decay)


def _frac(xs, pred) -> float:
    return (sum(1 for x in xs if pred(x)) / len(xs)) if xs else float("nan")


def _h_key(h: str) -> int:
    return int(h.lstrip("+").rstrip("d")) if h.lstrip("+").rstrip("d").isdigit() else 0
