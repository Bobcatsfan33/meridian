"""Option-chain snapshot source (ROADMAP §6). Real-first:
  - yfinance `option_chain` (free, DEFAULT) — real bid/ask/IV/OI; greeks computed locally,
  - Robinhood MCP (optional enhancement, interactive auth),
  - fixture (synthetic proxy) — for TESTS / explicit dev runs ONLY, never a silent default.

Every snapshot is tagged `data_source` ("live" | "fixture"). Fixture-sourced gamma output
is banner-flagged and tier-capped downstream so synthetic chains are never read as tradeable.
Source is chosen by config `adapters.options_source` (default "yfinance").
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass

from .gex import ChainContract

DEFAULT_SOURCE = "yfinance"
# Liquid default names for a live run when no --ticker / config list is given.
DEFAULT_LIVE_TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "JPM", "AMD", "SPY"]


@dataclass(frozen=True)
class ChainSnapshot:
    ticker: str
    spot: float
    iv_rank: float | None
    contracts: list[ChainContract]
    data_source: str = "live"   # "live" | "fixture"


def options_source(cfg) -> str:
    a = cfg.raw.get("adapters", {}) or {}
    return a.get("options_source") or (a.get("options", {}) or {}).get("source") or DEFAULT_SOURCE


def load_chain(cfg, target_date: dt.date, ticker: str) -> ChainSnapshot | None:
    src = options_source(cfg)
    if src == "robinhood":
        return _from_robinhood(cfg, target_date, ticker)
    if src == "fixture":
        return _from_fixture(cfg, target_date, ticker)
    return _from_yfinance(cfg, target_date, ticker)


def default_tickers(cfg, target_date: dt.date) -> list[str]:
    """Tickers to scan when none are passed explicitly."""
    src = options_source(cfg)
    if src == "fixture":
        return fixture_tickers(cfg, target_date)
    cfgd = (cfg.raw.get("adapters", {}).get("options", {}) or {}).get("tickers")
    return list(cfgd) if isinstance(cfgd, list) and cfgd else DEFAULT_LIVE_TICKERS


def fixture_tickers(cfg, target_date: dt.date) -> list[str]:
    d = _fixtures_dir(cfg) / target_date.isoformat()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def _fixtures_dir(cfg) -> pathlib.Path:
    rel = (cfg.raw.get("adapters", {}).get("options", {}) or {}).get(
        "fixtures_dir", "config/fixtures/options")
    return cfg.root / rel


def _from_fixture(cfg, target_date: dt.date, ticker: str) -> ChainSnapshot | None:
    path = _fixtures_dir(cfg) / target_date.isoformat() / f"{ticker}.json"
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    contracts = [
        ChainContract(
            strike=float(c["strike"]), expiry=dt.date.fromisoformat(c["expiry"]),
            is_call=(c["type"].lower() in ("c", "call")), open_interest=float(c["open_interest"]),
            iv=float(c["iv"]),
        )
        for c in d["contracts"]
    ]
    return ChainSnapshot(ticker=ticker, spot=float(d["spot"]),
                         iv_rank=d.get("iv_rank"), contracts=contracts, data_source="fixture")


def _from_yfinance(cfg, target_date: dt.date, ticker: str, max_expiries: int = 3) -> ChainSnapshot | None:
    """Real chain snapshot from yfinance (current chain; free options have no history).
    Greeks are computed locally from strike/expiry/IV via options/greeks.py downstream."""
    import math

    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        spot = _spot(t)
        if spot is None:
            return None
        expiries = list(t.options or [])[:max_expiries]
        contracts: list[ChainContract] = []
        for ex in expiries:
            try:
                chain = t.option_chain(ex)
            except Exception:
                continue
            exp = dt.date.fromisoformat(ex)
            for df, is_call in ((chain.calls, True), (chain.puts, False)):
                for _, row in df.iterrows():
                    iv = row.get("impliedVolatility")
                    oi = row.get("openInterest")
                    strike = row.get("strike")
                    if not iv or not oi or strike is None:
                        continue
                    if isinstance(iv, float) and math.isnan(iv):
                        continue
                    contracts.append(ChainContract(float(strike), exp, is_call, float(oi), float(iv)))
        if not contracts:
            return None
        return ChainSnapshot(ticker=ticker, spot=float(spot), iv_rank=None,
                             contracts=contracts, data_source="live")
    except Exception:
        return None


def _spot(t) -> float | None:
    try:
        lp = t.fast_info.get("lastPrice") if hasattr(t.fast_info, "get") else t.fast_info["lastPrice"]
        if lp:
            return float(lp)
    except Exception:
        pass
    try:
        h = t.history(period="1d")
        if len(h):
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def _from_robinhood(cfg, target_date: dt.date, ticker: str) -> ChainSnapshot | None:
    """Live Robinhood MCP path (priority 2). Best-effort; None if MCP/auth unavailable."""
    try:  # pragma: no cover - requires interactive MCP session
        from ..adapters.robinhood_mcp import fetch_chain_snapshot

        return fetch_chain_snapshot(target_date, ticker)
    except Exception:
        return None
