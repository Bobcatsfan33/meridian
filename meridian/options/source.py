"""Option-chain snapshot source. Free real-time options data does not exist (ROADMAP
§6), so the default source is a deterministic fixture; Robinhood MCP is the optional
live path (interactive auth) and degrades to None when unavailable.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass

from .gex import ChainContract


@dataclass(frozen=True)
class ChainSnapshot:
    ticker: str
    spot: float
    iv_rank: float | None
    contracts: list[ChainContract]


def load_chain(cfg, target_date: dt.date, ticker: str) -> ChainSnapshot | None:
    source = (cfg.raw.get("adapters", {}).get("options", {}) or {}).get("source", "fixture")
    if source == "robinhood":
        return _from_robinhood(cfg, target_date, ticker)
    return _from_fixture(cfg, target_date, ticker)


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
                         iv_rank=d.get("iv_rank"), contracts=contracts)


def _from_robinhood(cfg, target_date: dt.date, ticker: str) -> ChainSnapshot | None:
    """Live Robinhood MCP path (priority 2). Best-effort: returns None if the MCP
    tools/auth are unavailable (e.g. headless cron). Snapshot-only (no historical)."""
    try:  # pragma: no cover - requires interactive MCP session
        from ..adapters.robinhood_mcp import fetch_chain_snapshot

        return fetch_chain_snapshot(target_date, ticker)
    except Exception:
        return None
