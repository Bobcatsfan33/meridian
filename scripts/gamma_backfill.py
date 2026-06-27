"""Backfill gamma-squeeze firings across a date range (Phase 6 demo support).

For each weekday in [start,end]: generate deterministic option fixtures (spot from the
already-ingested DailyBar), ingest options, re-featurize that date, and additively match
the gamma_squeeze pattern. Requires `meridian backfill` to have run first (so price
events + DailyBar spots exist for each date).

Usage: python scripts/gamma_backfill.py 2026-05-18 2026-05-29 NVDA MSFT ON WDC STX JPM TTD
"""
from __future__ import annotations

import datetime as dt
import json
import sys

from meridian.config import Config
from meridian.engine.featurize_run import run_featurize
from meridian.engine.match import run_match
from meridian.options.ingest import run_options
from meridian.storage import connect

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from gen_option_fixtures import build_chain, spot_for  # noqa: E402


def main() -> None:
    start = dt.date.fromisoformat(sys.argv[1])
    end = dt.date.fromisoformat(sys.argv[2])
    tickers = sys.argv[3:]
    cfg = Config.load()
    d = start
    while d <= end:
        if d.weekday() < 5:
            _one_day(cfg, d, tickers)
        d += dt.timedelta(days=1)


def _one_day(cfg: Config, d: dt.date, tickers: list[str]) -> None:
    out_dir = cfg.root / "config" / "fixtures" / "options" / d.isoformat()
    con = connect(cfg.duckdb_path)
    made = 0
    try:
        for t in tickers:
            spot = spot_for(con, t, d)
            if spot is None:
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{t}.json").write_text(json.dumps(build_chain(spot, d)))
            made += 1
    finally:
        con.close()
    if made == 0:
        print(f"  {d}: no spots (skipped)")
        return
    run_options(cfg, d, tickers=tickers)
    run_featurize(cfg, d)
    res = run_match(cfg, d, pattern_ids=["gamma_squeeze"])
    print(f"  {d}: gamma_squeeze firings={res.n_firings}")


if __name__ == "__main__":
    main()
