"""`meridian demo` — a self-contained, OFFLINE, deterministic end-to-end run.

A brand-new cloner runs `meridian demo` and sees real output in one command: no API keys,
no network. It seeds a tiny committed fixture (a few real-universe IT names + a sector ETF,
~40 days of bars) into a sample DB, then runs the genuine engine path — featurize (L1) →
match (L2) → build explanations + postmortem (the deterministic Jinja layer). Idempotent.

This is demo scaffolding only; it calls the real engine functions and changes no engine logic.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from dataclasses import dataclass, field

from .config import Config
from .ingest.clock import market_close_utc
from .storage import connect, init_db

DEMO_DATE = dt.date(2026, 6, 26)
# Real S&P names (all Information Technology) so sector maps + XLK resolve from the universe.
_STOCKS = ["AAPL", "MSFT", "NVDA"]
_SECTOR_ETF = "XLK"
# Deterministic day-D returns: AAPL is the abnormal mover; the sector lifts modestly.
_DAY_RETURN = {"AAPL": 0.08, "MSFT": 0.012, "NVDA": 0.031, "XLK": 0.02}


@dataclass
class DemoResult:
    db_path: str
    date: dt.date
    n_events: int = 0
    n_firings: int = 0
    n_cards: int = 0
    top: list[tuple[str, str, float]] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


def run_demo(db_path: str | None = None) -> DemoResult:
    cfg = Config.load()
    db = pathlib.Path(db_path) if db_path else (cfg.root / "data" / "demo.duckdb")
    init_db(db, cfg.universe_file)                      # sample DB w/ real universe
    cfg.raw.setdefault("storage", {})["duckdb_path"] = str(db)

    _seed_fixture(cfg)
    res = DemoResult(db_path=str(db), date=DEMO_DATE)
    res.steps.append("seed")

    from .engine.featurize import featurize
    from .engine.match import run_match
    from .outputs.build import build_explanations
    from .outputs.postmortem import build_context

    con = connect(db)
    try:
        res.n_events = con.execute(
            "SELECT count(*) FROM normalized_events WHERE event_time::date=?", [DEMO_DATE]).fetchone()[0]
        featurize(con, cfg, DEMO_DATE)
    finally:
        con.close()
    res.steps.append("featurize")

    res.n_firings = run_match(cfg, DEMO_DATE).n_firings
    res.steps.append("match")

    evidences = build_explanations(cfg, DEMO_DATE)
    res.n_cards = len(evidences)
    build_context(cfg, DEMO_DATE, evidences)            # postmortem context (deterministic)
    res.steps.append("explanations+postmortem")

    res.top = sorted(
        ((e["ticker"], e["pattern"]["id"], e["confidence"]["value"]) for e in evidences),
        key=lambda x: -x[2])[:5]
    return res


def _seed_fixture(cfg: Config) -> None:
    """Write a deterministic offline fixture: ~40 days of bars (ticker_state) + day-D events,
    regime, and the expected-behavior baseline. Idempotent (wipes the date first)."""
    con = connect(cfg.duckdb_path)
    try:
        close = market_close_utc(DEMO_DATE).replace(tzinfo=None)
        symbols = _STOCKS + [_SECTOR_ETF]
        # wipe any prior demo seed for these symbols / this date (idempotent re-run)
        ph = ",".join("?" * len(symbols))
        con.execute(f"DELETE FROM ticker_state_1m WHERE ticker IN ({ph})", symbols)
        con.execute("DELETE FROM normalized_events WHERE event_time::date=?", [DEMO_DATE])
        con.execute("DELETE FROM graded_events WHERE event_time::date=?", [DEMO_DATE])
        con.execute("DELETE FROM regimes_daily WHERE trade_date=?", [DEMO_DATE])
        con.execute("DELETE FROM expected_behavior_1m WHERE ts=?", [close])

        # 40 trailing days of small deterministic returns + the day-D return
        for sym in symbols:
            for i in range(40, 0, -1):
                ts = market_close_utc(DEMO_DATE - dt.timedelta(days=i)).replace(tzinfo=None)
                ret = 0.004 if i % 2 else -0.003
                con.execute("INSERT INTO ticker_state_1m (ticker, ts, close, ret_1m, rel_volume) "
                            "VALUES (?,?,?,?,?)", [sym, ts, 100.0, ret, 1.0])
            con.execute("INSERT INTO ticker_state_1m (ticker, ts, close, ret_1m, rel_volume) "
                        "VALUES (?,?,?,?,?)", [sym, close, 108.0, _DAY_RETURN[sym], 2.4])

        # day-D normalized events: DailyBar per stock, ETFBar for the sector (data_source=fixture)
        rows = []
        for sym in _STOCKS:
            rows.append((f"d_{sym}", sym, "DailyBar", "price_volume",
                         json.dumps({"ret_1m": _DAY_RETURN[sym], "rel_volume_pctile": 0.95})))
        rows.append((f"d_{_SECTOR_ETF}", _SECTOR_ETF, "ETFBar", "sector_peer",
                     json.dumps({"ret_1m": _DAY_RETURN[_SECTOR_ETF]})))
        for eid, sym, etype, fam, payload in rows:
            sector = "Information Technology" if sym in _STOCKS else None
            con.execute(
                "INSERT OR REPLACE INTO normalized_events (event_id,event_time,ingest_time,ticker,"
                "event_type,family,source,confidence,sector,related_symbols,parent_event_id,"
                "data_source,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [eid, close, close, sym, etype, fam, "demo", 0.95, sector, [], None, "fixture", payload])

        con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                    [DEMO_DATE, "mid_vol_range", ["mid_vol", "range", "broad_advance"]])
        for sym in _STOCKS:
            con.execute("INSERT INTO expected_behavior_1m (ticker, ts, abnormal_ret, beta) VALUES (?,?,?,?)",
                        [sym, close, _DAY_RETURN[sym] - 0.01, 1.0])
    finally:
        con.close()
