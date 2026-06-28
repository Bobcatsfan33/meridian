"""Options ingestion (Phase 5): chain snapshots -> gex_surface + options_state_1m +
dealer_pos normalized_events. Free-first/fixture by default; Robinhood MCP optional.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from ..adapters.base import make_event_id
from ..config import Config
from ..ingest.clock import UTC, market_close_utc
from ..storage import connect
from .events import derive_events
from .gex import build_surface
from .source import ChainSnapshot, default_tickers, load_chain

RELIABILITY = 0.70  # snapshot proxy (no paid feed): data-quality confidence


@dataclass
class OptionsSummary:
    target_date: dt.date
    n_tickers: int = 0
    n_events: int = 0
    n_surface_rows: int = 0
    event_type_counts: dict[str, int] = field(default_factory=dict)
    tickers: list[str] = field(default_factory=list)


def run_options(cfg: Config, target_date: dt.date, tickers: list[str] | None = None,
                now: dt.datetime | None = None) -> OptionsSummary:
    now = now or dt.datetime.now(UTC)
    r = float((cfg.raw.get("adapters", {}).get("options", {}) or {}).get("risk_free_rate", 0.0))
    targets = tickers or default_tickers(cfg, target_date)
    close_ts = market_close_utc(target_date).replace(tzinfo=None)

    con = connect(cfg.duckdb_path)
    summary = OptionsSummary(target_date=target_date)
    try:
        _wipe(con, target_date, close_ts)
        norm_rows, raw_rows, surf_rows, state_rows = [], [], [], []
        for ticker in targets:
            snap = load_chain(cfg, target_date, ticker)
            if snap is None or not snap.contracts:
                continue
            surface = build_surface(target_date, snap.spot, snap.contracts, r=r)
            specs = derive_events(snap, surface)
            if not specs:
                continue
            summary.n_tickers += 1
            summary.tickers.append(ticker)
            src_label = f"options_{snap.data_source}"
            for spec in specs:
                eid = make_event_id("options", spec["event_type"], ticker, market_close_utc(target_date))
                spec_payload = {**spec["payload"], "data_source": snap.data_source}
                payload = json.dumps(spec_payload, default=str)
                norm_rows.append((eid, close_ts, now.astimezone(UTC).replace(tzinfo=None), ticker,
                                  spec["event_type"], "dealer_pos", src_label,
                                  RELIABILITY, None, [], None, payload))
                raw_rows.append((f"raw_{eid}", now.astimezone(UTC).replace(tzinfo=None),
                                 src_label, ticker, payload))
                summary.event_type_counts[spec["event_type"]] = \
                    summary.event_type_counts.get(spec["event_type"], 0) + 1
            for s in surface.per_strike:
                surf_rows.append((ticker, close_ts, s.strike, None, None,
                                  s.call_oi + s.put_oi, s.dealer_gamma, snap.data_source))
            state_rows.append((ticker, close_ts, _atm_iv(snap), snap.iv_rank,
                               surface.net_gex, surface.gamma_flip, surface.call_wall, surface.put_wall))

        _write(con, norm_rows, raw_rows, surf_rows, state_rows)
        summary.n_events = len(norm_rows)
        summary.n_surface_rows = len(surf_rows)
        return summary
    finally:
        con.close()


def _atm_iv(snap: ChainSnapshot) -> float | None:
    calls = [c for c in snap.contracts if c.is_call]
    if not calls:
        return None
    return min(calls, key=lambda c: abs(c.strike - snap.spot)).iv


def _wipe(con, target_date, close_ts) -> None:
    ids = [r[0] for r in con.execute(
        "SELECT event_id FROM normalized_events WHERE family='dealer_pos' "
        "AND CAST(event_time AS DATE)=?", [target_date]).fetchall()]
    if ids:
        ph = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM normalized_events WHERE event_id IN ({ph})", ids)
        con.execute(f"DELETE FROM graded_events WHERE event_id IN ({ph})", ids)
    con.execute("DELETE FROM gex_surface WHERE ts=?", [close_ts])
    con.execute("DELETE FROM options_state_1m WHERE ts=?", [close_ts])


def _write(con, norm_rows, raw_rows, surf_rows, state_rows) -> None:
    if norm_rows:
        con.executemany(
            "INSERT OR REPLACE INTO normalized_events (event_id,event_time,ingest_time,ticker,"
            "event_type,family,source,confidence,sector,related_symbols,parent_event_id,payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", norm_rows)
        con.executemany(
            "INSERT OR REPLACE INTO raw_market_events (event_id,ingest_time,source,ticker,payload) "
            "VALUES (?,?,?,?,?)", raw_rows)
    if surf_rows:
        con.executemany(
            "INSERT INTO gex_surface (ticker,ts,strike,expiry,gamma,open_interest,dealer_gamma,"
            "data_source) VALUES (?,?,?,?,?,?,?,?)", surf_rows)
    if state_rows:
        con.executemany(
            "INSERT INTO options_state_1m (ticker,ts,iv,iv_pctile,net_gex,gamma_flip,call_wall,put_wall) "
            "VALUES (?,?,?,?,?,?,?,?)", state_rows)
