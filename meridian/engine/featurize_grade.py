"""L1 grading kernels: continuous (own-name percentile) and discrete (own-name rarity).

Both grade an event against THIS name's own trailing baseline, never a global cutoff.
Thresholds/params are passed in from config.featurization. Deterministic; golden-tested.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ..state import baseline as bl


@dataclass(frozen=True)
class GradeResult:
    abnormality: float
    insufficient: bool
    method: str
    components: dict[str, Any] = field(default_factory=dict)


def grade_continuous(con, ticker: str, close_ts, win: int, min_hist: int, insuff: float) -> GradeResult:
    """Abnormality = percentile rank of |ret_1m(D)| within the name's trailing |ret_1m|."""
    today = con.execute(
        "SELECT ret_1m, rel_volume FROM ticker_state_1m WHERE ticker = ? AND ts = ?",
        [ticker, close_ts],
    ).fetchone()
    if not today or today[0] is None:
        return GradeResult(insuff, True, "no_state", {"reason": "no_target_state"})
    ret_today, rel_vol = today[0], today[1]

    trailing = [
        r[0]
        for r in con.execute(
            "SELECT ret_1m FROM ticker_state_1m WHERE ticker = ? AND ts < ? "
            "ORDER BY ts DESC LIMIT ?",
            [ticker, close_ts, win],
        ).fetchall()
        if r[0] is not None
    ]
    rv_trailing = [
        r[0]
        for r in con.execute(
            "SELECT rel_volume FROM ticker_state_1m WHERE ticker = ? AND ts < ? "
            "ORDER BY ts DESC LIMIT ?",
            [ticker, close_ts, win],
        ).fetchall()
        if r[0] is not None
    ]
    components: dict[str, Any] = {"ret_1m": ret_today, "rel_volume": rel_vol, "n_history": len(trailing)}
    if rel_vol is not None and rv_trailing:
        components["rel_volume_pctile"] = bl.percentile_rank(rel_vol, rv_trailing)

    if len(trailing) < min_hist:
        return GradeResult(insuff, True, "insufficient_history", components)
    abn = bl.abnormality_from_magnitude(ret_today, trailing)
    components["move_pctile"] = abn
    return GradeResult(abn, False, "own_regime_percentile", components)


def grade_options(ev: dict, opt_cfg: dict) -> GradeResult:
    """Grade a dealer-positioning event from its raw measures (ROADMAP §9 — thresholds
    live here in L1). Self-normalizing measures so no IV/GEX history is required."""
    et = ev["event_type"]
    p = ev.get("payload") or {}
    into_pct = float(opt_cfg.get("spot_into_strike_pct", 0.03))
    neutral_iv = float(opt_cfg.get("neutral_iv_rank", 0.5))

    if et == "ShortGamma":
        abn = _clamp01(-float(p.get("net_gex_ratio", 0.0)))
    elif et == "SpotIntoStrike":
        dist = float(p.get("dist_ratio", 1.0))
        abn = _clamp01(1.0 - dist / into_pct) if into_pct > 0 else 0.0
    elif et == "IVExpansion":
        ivr = p.get("iv_rank")
        abn = float(ivr) if isinstance(ivr, (int, float)) else neutral_iv
    elif et == "GammaFlip":
        abn = _clamp01(float(p.get("flip_proximity", 0.0)))
    elif et in ("CallWall", "PutWall"):
        abn = _clamp01(float(p.get("concentration", 0.0)))
    else:
        abn = 0.5
    return GradeResult(round(abn, 6), False, "dealer_positioning", {**p, "measure_event": et})


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def grade_equity_flow(con, ev: dict, close_ts, win: int, min_hist: int, insuff: float) -> GradeResult:
    """Grade FINRA short-volume / dark-pool against the name's OWN trailing baseline
    (percentile of the level vs its history, dates < close). Higher level => higher
    abnormality. Thresholds live here in L1, not in the adapter."""
    et = ev["event_type"]
    p = ev.get("payload") or {}
    col = "short_pct" if et == "ShortVolumeSpike" else "off_exchange_share"
    value = p.get(col)
    if value is None:
        return GradeResult(insuff, True, "no_measure", {"reason": f"no_{col}"})
    trailing = [
        r[0] for r in con.execute(
            f"SELECT {col} FROM equity_flow_state WHERE ticker=? AND ts<? AND {col} IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?", [ev["ticker"], close_ts, win]).fetchall()
        if r[0] is not None
    ]
    components = {col: value, "n_history": len(trailing)}
    if len(trailing) < min_hist:
        return GradeResult(insuff, True, "insufficient_history", components)
    abn = bl.percentile_rank(float(value), trailing)
    components["level_pctile"] = abn
    return GradeResult(abn, False, "own_flow_percentile", components)


def grade_discrete(con, ev: dict, target_date: dt.date, win: int, priors: dict) -> GradeResult:
    """Abnormality from the rarity of this (ticker, family) in the trailing window.

    Never seen for this name -> the family prior; frequent -> downgraded toward 0.1.
    """
    fam = ev["family"]
    prior = float(priors.get(fam, priors.get("default", 0.6)))
    ticker = ev["ticker"]
    if not ticker:
        return GradeResult(prior, False, "family_prior", {"reason": "no_ticker", "prior": prior})

    start = target_date - dt.timedelta(days=int(win * 1.6))
    count = con.execute(
        "SELECT count(DISTINCT CAST(event_time AS DATE)) FROM normalized_events "
        "WHERE ticker = ? AND family = ? AND CAST(event_time AS DATE) >= ? "
        "AND CAST(event_time AS DATE) < ?",
        [ticker, fam, start, target_date],
    ).fetchone()[0]
    components = {"trailing_days_with_family": int(count), "prior": prior}
    if count == 0:
        return GradeResult(prior, True, "family_prior_no_history", components)
    base_rate = count / float(win)
    abn = max(0.1, min(prior, prior * (1.0 - base_rate)))
    components["base_rate"] = base_rate
    return GradeResult(abn, False, "own_rarity", components)
