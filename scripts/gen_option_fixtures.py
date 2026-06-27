"""Generate deterministic option-chain fixtures for the Phase 5 gamma-squeeze demo.

Free real-time options data does not exist (ROADMAP §6); this builds a plausible,
fully deterministic chain snapshot grounded in the real close (spot) already in the
DB. The chain is shaped as a short-gamma-into-call-wall setup so the differentiator
pattern can be exercised end to end. NOT market options data — a labelled proxy.

Usage: python scripts/gen_option_fixtures.py 2026-06-26 NVDA MSFT ON
"""
from __future__ import annotations

import datetime as dt
import json
import sys

from meridian.config import Config
from meridian.storage import connect


def spot_for(con, ticker: str, date: dt.date) -> float | None:
    row = con.execute(
        "SELECT payload FROM normalized_events WHERE ticker=? AND event_type='DailyBar' "
        "AND CAST(event_time AS DATE)=?", [ticker, date]).fetchone()
    if not row:
        return None
    return float(json.loads(row[0])["close"])


def build_chain(spot: float, date: dt.date) -> dict:
    front = date + dt.timedelta(days=14)
    nxt = date + dt.timedelta(days=42)
    contracts = []
    # strikes from -10% to +10% of spot in 1% steps (fine grid near spot)
    steps = [round(spot * (1 + k / 100.0), 2) for k in range(-10, 11)]
    call_wall = round(spot * 1.01, 2)  # wall just above spot -> SpotIntoStrike
    for strike in steps:
        for expiry in (front, nxt):
            # Heavy PUT open interest below spot -> dealers net short gamma (net_gex<0).
            put_oi = 9000 if strike < spot else 1500
            # A concentrated CALL wall ~2% above spot.
            call_oi = 12000 if abs(strike - call_wall) < 0.01 else (3000 if strike > spot else 800)
            iv = 0.55 if expiry == front else 0.48  # elevated front IV
            contracts.append({"strike": strike, "expiry": expiry.isoformat(),
                              "type": "put", "open_interest": put_oi, "iv": iv})
            contracts.append({"strike": strike, "expiry": expiry.isoformat(),
                              "type": "call", "open_interest": call_oi, "iv": iv})
    return {"spot": spot, "iv_rank": 0.85, "contracts": contracts}


def main() -> None:
    date = dt.date.fromisoformat(sys.argv[1])
    tickers = sys.argv[2:]
    cfg = Config.load()
    out_dir = cfg.root / "config" / "fixtures" / "options" / date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    con = connect(cfg.duckdb_path)
    try:
        for t in tickers:
            spot = spot_for(con, t, date)
            if spot is None:
                print(f"  {t}: no spot in DB (ingest {date} first) — skipped")
                continue
            (out_dir / f"{t}.json").write_text(json.dumps(build_chain(spot, date), indent=2))
            print(f"  {t}: spot={spot} -> {out_dir / (t + '.json')}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
