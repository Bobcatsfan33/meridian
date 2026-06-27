"""GEX proxy from an option-chain snapshot (ROADMAP §6, §10 gamma squeeze).

Dealer gamma model (the standard retail proxy): dealers are assumed long call gamma
and short put gamma. Per strike:
    dealer_gamma = (call_OI*gamma_call - put_OI*gamma_put) * contract_mult * spot
Net GEX < 0 => dealers net short gamma (hedging AMPLIFIES moves — the squeeze setup).
gamma_flip is the strike where cumulative dealer gamma crosses zero. Pure & golden-tested.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import greeks

CONTRACT_MULT = 100.0


@dataclass(frozen=True)
class ChainContract:
    strike: float
    expiry: dt.date
    is_call: bool
    open_interest: float
    iv: float


@dataclass(frozen=True)
class StrikeGex:
    strike: float
    dealer_gamma: float
    call_oi: float
    put_oi: float


@dataclass
class GexSurface:
    spot: float
    net_gex: float
    gross_gex: float
    gamma_flip: float | None
    call_wall: float | None
    put_wall: float | None
    per_strike: list[StrikeGex] = field(default_factory=list)

    @property
    def net_gex_ratio(self) -> float:
        """Signed share of gross gamma that is net (in [-1,1]); self-normalizing, no history."""
        return self.net_gex / self.gross_gex if self.gross_gex else 0.0


def build_surface(as_of: dt.date, spot: float, contracts: list[ChainContract], r: float = 0.0) -> GexSurface:
    by_strike: dict[float, dict[str, float]] = {}
    call_gamma_oi: dict[float, float] = {}
    put_gamma_oi: dict[float, float] = {}
    for c in contracts:
        t = greeks.year_fraction(as_of, c.expiry)
        g = greeks.gamma(spot, c.strike, t, c.iv, r)
        rec = by_strike.setdefault(c.strike, {"call_oi": 0.0, "put_oi": 0.0, "dealer_gamma": 0.0})
        contrib = g * c.open_interest * CONTRACT_MULT * spot
        if c.is_call:
            rec["call_oi"] += c.open_interest
            rec["dealer_gamma"] += contrib
            call_gamma_oi[c.strike] = call_gamma_oi.get(c.strike, 0.0) + g * c.open_interest
        else:
            rec["put_oi"] += c.open_interest
            rec["dealer_gamma"] -= contrib
            put_gamma_oi[c.strike] = put_gamma_oi.get(c.strike, 0.0) + g * c.open_interest

    per_strike = [StrikeGex(k, v["dealer_gamma"], v["call_oi"], v["put_oi"])
                  for k, v in sorted(by_strike.items())]
    net_gex = sum(s.dealer_gamma for s in per_strike)
    gross_gex = sum(abs(s.dealer_gamma) for s in per_strike)

    call_wall = max(call_gamma_oi, key=call_gamma_oi.get) if call_gamma_oi else None
    put_wall = max(put_gamma_oi, key=put_gamma_oi.get) if put_gamma_oi else None
    gamma_flip = _flip(per_strike)

    return GexSurface(spot=spot, net_gex=net_gex, gross_gex=gross_gex, gamma_flip=gamma_flip,
                      call_wall=call_wall, put_wall=put_wall, per_strike=per_strike)


def _flip(per_strike: list[StrikeGex]) -> float | None:
    """Strike where cumulative dealer gamma crosses zero (ascending in strike)."""
    cum = 0.0
    prev_strike = None
    prev_cum = 0.0
    for s in per_strike:
        cum += s.dealer_gamma
        if prev_strike is not None and (prev_cum < 0 <= cum or prev_cum > 0 >= cum):
            return round((prev_strike + s.strike) / 2.0, 4)
        prev_strike, prev_cum = s.strike, cum
    return None
