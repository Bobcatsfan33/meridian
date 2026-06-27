"""Derive dealer-positioning events from a GEX surface (ROADMAP §7 dealer_pos family).

Emits ShortGamma / SpotIntoStrike / IVExpansion / GammaFlip / CallWall / PutWall with
RAW MEASURES only (net_gex_ratio, dist_ratio, iv_rank, ...). Abnormality is graded in
L1 (grade_options) — no thresholds here. Pure & golden-tested.
"""
from __future__ import annotations

from typing import Any

from .gex import GexSurface
from .source import ChainSnapshot


def _atm_iv(snap: ChainSnapshot) -> float | None:
    calls = [c for c in snap.contracts if c.is_call]
    if not calls:
        return None
    nearest = min(calls, key=lambda c: abs(c.strike - snap.spot))
    return nearest.iv


def derive_events(snap: ChainSnapshot, surface: GexSurface) -> list[dict[str, Any]]:
    spot = surface.spot
    out: list[dict[str, Any]] = []

    if surface.net_gex < 0:
        out.append({"event_type": "ShortGamma", "payload": {
            "net_gex": surface.net_gex, "net_gex_ratio": surface.net_gex_ratio,
            "gamma_flip": surface.gamma_flip}})

    if surface.call_wall is not None and spot:
        dist = abs(spot - surface.call_wall) / spot
        out.append({"event_type": "SpotIntoStrike", "payload": {
            "spot": spot, "wall": surface.call_wall, "dist_ratio": dist}})

    atm_iv = _atm_iv(snap)
    if atm_iv is not None:
        out.append({"event_type": "IVExpansion", "payload": {
            "atm_iv": atm_iv, "iv_rank": snap.iv_rank}})

    if surface.gamma_flip is not None and spot:
        prox = max(0.0, 1.0 - abs(spot - surface.gamma_flip) / spot)
        out.append({"event_type": "GammaFlip", "payload": {
            "gamma_flip": surface.gamma_flip, "flip_proximity": prox}})

    pos = sum(s.dealer_gamma for s in surface.per_strike if s.dealer_gamma > 0) or 1.0
    neg = sum(-s.dealer_gamma for s in surface.per_strike if s.dealer_gamma < 0) or 1.0
    if surface.call_wall is not None:
        wall_g = sum(s.dealer_gamma for s in surface.per_strike if s.strike == surface.call_wall)
        out.append({"event_type": "CallWall", "payload": {
            "call_wall": surface.call_wall, "concentration": max(0.0, wall_g) / pos}})
    if surface.put_wall is not None:
        wall_g = sum(-s.dealer_gamma for s in surface.per_strike if s.strike == surface.put_wall)
        out.append({"event_type": "PutWall", "payload": {
            "put_wall": surface.put_wall, "concentration": max(0.0, wall_g) / neg}})

    return out
