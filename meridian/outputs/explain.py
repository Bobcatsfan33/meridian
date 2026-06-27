"""Build the structured evidence object for one firing (ROADMAP §11, §13, §15).

This object is the SINGLE SOURCE OF TRUTH for the card: the Jinja layer may print only
fields present here. Carries drivers (attribution), the capped confidence tier, the
unexplained residual (never zero), the partial-order timeline, and the invalidation line.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..engine import constraints
from ..engine.mechanical import classify
from ..engine.scoring import DriverInput, score
from ..engine.structural import MatchEvent
from . import phrases

# Invalidation lines per pattern (deterministic templates; ROADMAP §11 falsifiability).
INVALIDATIONS = {
    "sector_sympathy": "Read weakens if {ticker} decouples from {sector_etf}, or a "
                       "company-specific catalyst (news/filing) emerges.",
    "options_led_proxy": "Read weakens if a news/filing catalyst surfaces (move was "
                         "informational, not flow), or relative volume normalizes with no follow-through.",
    "price_before_news": "Read weakens if the initial move reverses before the headline's "
                         "follow-through, or the headline proves unrelated to the move.",
    "gamma_squeeze": "Read weakens if IV bleeds while spot holds, dealer gamma normalizes "
                     "(flip clears), or the move loses VWAP as hedging flow fades.",
}
_DEFAULT_INVALIDATION = "Read weakens if the supporting evidence reverses or a stronger catalyst emerges."


def driver_inputs(pattern_id: str, bindings: dict[str, MatchEvent | None], ticker: str) -> list[DriverInput]:
    """Map a pattern's bound events to named driver contributions (graded weights)."""
    out: list[DriverInput] = []
    p = bindings.get("P")
    if pattern_id == "sector_sympathy":
        s = bindings.get("S")
        if p:
            out.append(DriverInput(f"Abnormal move in {ticker}", p.abnormality))
        if s:
            out.append(DriverInput(f"Sector move in {s.ticker}", s.abnormality))
    elif pattern_id == "options_led_proxy":
        if p:
            out.append(DriverInput("Abnormal price move", p.abnormality))
            rv = p.payload.get("rel_volume_pctile")
            if isinstance(rv, (int, float)):
                out.append(DriverInput("Relative-volume expansion", float(rv)))
    elif pattern_id == "price_before_news":
        n = bindings.get("N")
        if p:
            out.append(DriverInput("Abnormal price move", p.abnormality))
        if n:
            out.append(DriverInput("Late headline confirmation", n.abnormality))
    elif pattern_id == "gamma_squeeze":
        g, k, v = bindings.get("G"), bindings.get("K"), bindings.get("V")
        if g:
            out.append(DriverInput("Dealers short gamma", g.abnormality))
        if k:
            out.append(DriverInput("Spot into strike cluster", k.abnormality))
        if v:
            out.append(DriverInput("IV expansion (hedging)", v.abnormality))
        if p:
            out.append(DriverInput("Price expansion", p.abnormality))
    else:
        if p:
            out.append(DriverInput("Abnormal move", p.abnormality))
    return out


def _timeline(events: list[MatchEvent], demote_news: bool) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for ev in events:
        if ev is None or ev.event_id in seen:
            continue
        seen.add(ev.event_id)
        label = _label(ev)
        if demote_news and ev.family == "news":
            label = "LATE confirmation — " + label
        rows.append({
            "time": ev.event_time.strftime("%H:%M") if isinstance(ev.event_time, dt.datetime) else str(ev.event_time),
            "event_time": ev.event_time.isoformat() if isinstance(ev.event_time, dt.datetime) else str(ev.event_time),
            "family": ev.family,
            "label": label,
            "abnormality": round(ev.abnormality, 4),
        })
    rows.sort(key=lambda r: r["event_time"])  # partial order by aligned event_time
    return rows


def _label(ev: MatchEvent) -> str:
    p = ev.payload
    if ev.family == "price_volume":
        r = p.get("ret_1m")
        return f"{ev.ticker} abnormal move {r:+.2%}" if isinstance(r, (int, float)) else f"{ev.ticker} abnormal move"
    if ev.family == "sector_peer":
        return f"{ev.ticker} sector basket move"
    if ev.family == "news":
        h = p.get("headline") or "headline"
        return f"Headline: {h[:70]}"
    if ev.family == "filing":
        return f"{p.get('form_type', 'filing')} filed"
    if ev.family == "macro":
        return f"{ev.ticker} macro print"
    return f"{ev.event_type} ({ev.ticker})"


def build_evidence(
    *,
    ticker: str,
    pattern_id: str,
    pattern_ver: str,
    pattern_desc: str,
    completeness: float,
    bindings: dict[str, MatchEvent | None],
    move_pct: float | None,
    abnormal_move_pct: float | None,
    regime_tags: list[str],
    sector_etf: str | None,
    lead_lag_strength: float,
    insufficient_history: bool,
    feeds_ok: bool,
    cfg_scoring: dict,
    window_start: Any,
    window_end: Any,
    catalysts: list[MatchEvent] | None = None,
    explained_fraction: float | None = None,
    residual_basis: str = "structural",
) -> dict[str, Any]:
    catalysts = catalysts or []
    move_class = classify(bindings, catalysts)
    drivers_in = driver_inputs(pattern_id, bindings, ticker)
    corroboration = len(drivers_in)
    sr = score(
        completeness=completeness, drivers=drivers_in, corroboration_count=corroboration,
        lead_lag_strength=lead_lag_strength, cfg_scoring=cfg_scoring,
        explained_fraction=explained_fraction, residual_basis=residual_basis,
    )
    sector_abn = bindings["S"].abnormality if bindings.get("S") else None
    target_abn = bindings["P"].abnormality if bindings.get("P") else 0.0
    co = constraints.apply(
        pattern_id=pattern_id, tier=sr.tier, cfg_scoring=cfg_scoring,
        target_abnormality=target_abn, sector_abnormality=sector_abn,
        insufficient_history=insufficient_history, feeds_ok=feeds_ok,
    )
    inval = INVALIDATIONS.get(pattern_id, _DEFAULT_INVALIDATION).format(
        ticker=ticker, sector_etf=sector_etf or "its sector",
    )
    drivers = [{"driver": d.driver, "weight": d.weight, "inputs": d.inputs} for d in sr.drivers]
    return {
        "ticker": ticker,
        "window_start": str(window_start),
        "window_end": str(window_end),
        "pattern": {"id": pattern_id, "version": pattern_ver, "description": pattern_desc,
                    "completeness": round(completeness, 4)},
        "move_pct": move_pct,
        "abnormal_move_pct": abnormal_move_pct,
        "confidence": {"value": sr.confidence, "tier": co.tier},
        "tier_phrase": phrases.tier_phrase(co.tier),
        "tier_verb": phrases.tier_verb(co.tier),
        "readout": phrases.readout(pattern_id),
        "drivers": drivers,
        "unexplained_residual": sr.residual,
        "residual_basis": sr.residual_basis,
        "constraints_applied": co.notes,
        "regime_tags": list(regime_tags),
        "move_class": move_class.label,
        "move_class_reason": move_class.reason,
        "timeline": _timeline(list(bindings.values()) + catalysts, move_class.demote_news),
        "invalidation": inval,
        "not_investment_advice": True,
    }
