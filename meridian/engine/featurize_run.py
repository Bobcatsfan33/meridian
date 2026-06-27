"""Phase 2 orchestration: fetch trailing history -> build state -> grade (L1).

Thin glue used by `meridian featurize`. Network fetch (prices/FRED) is isolated in
state.prices so the pure state+grade math stays deterministic and golden-testable.
"""
from __future__ import annotations

import datetime as dt

from ..config import Config
from ..storage import connect
from ..state import build_state
from ..state.prices import fetch_fred_window, fetch_yf_window
from .featurize import FeaturizeSummary, featurize


def build_symbol_meta(cfg: Config, con) -> tuple[dict[str, dict], dict[str, str]]:
    """Return (symbol_meta, fred_series). symbol_meta keyed by symbol."""
    import csv

    meta: dict[str, dict] = {}
    rows = con.execute("SELECT symbol, name, sector FROM universe").fetchall()
    for sym, _name, sector in rows:
        meta[sym] = {"kind": "stock", "role": "stock", "sector": sector}

    if cfg.index_etf_file.exists():
        with cfg.index_etf_file.open() as fh:
            for r in csv.DictReader(fh):
                role = (r.get("role") or "sector").strip()
                meta[r["symbol"]] = {
                    "kind": "macro" if role == "macro" else "etf",
                    "role": role,
                    "sector_name": (r.get("description") or "").strip(),
                    "sector": None,
                }

    from ..adapters.fred import DEFAULT_SERIES

    fred_cfg = (cfg.raw.get("adapters", {}) or {}).get("fred", {}) or {}
    series = fred_cfg.get("series") if isinstance(fred_cfg.get("series"), dict) else None
    fred_series = series or DEFAULT_SERIES
    for sid in fred_series:
        meta.setdefault(sid, {"kind": "macro", "role": "macro", "sector": None})
    return meta, fred_series


def run_featurize(cfg: Config, target_date: dt.date) -> tuple[object, FeaturizeSummary]:
    con = connect(cfg.duckdb_path)
    try:
        meta, fred_series = build_symbol_meta(cfg, con)
        cal_days = int(cfg.feat("history_calendar_days", 200))
        start = target_date - dt.timedelta(days=cal_days)

        yf_symbols = [s for s, m in meta.items() if m["kind"] in ("stock", "etf", "macro")
                      and not _is_fred(s, fred_series)]
        price_window = fetch_yf_window(yf_symbols, start, target_date)
        fred_cfg = (cfg.raw.get("adapters", {}) or {}).get("fred", {}) or {}
        price_window.update(fetch_fred_window(fred_series, start, target_date, fred_cfg))

        state = build_state(con, cfg, target_date, price_window, meta)
        summ = featurize(con, cfg, target_date)
        return state, summ
    finally:
        con.close()


def _is_fred(symbol: str, fred_series: dict) -> bool:
    return symbol in fred_series
