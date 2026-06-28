"""Ingest pipeline: run enabled adapters for one trade date -> normalized_events.

Flow: load universe+ETFs -> build IngestContext -> run each adapter (fetch+normalize,
never fatal on a single feed) -> dedup by event_id -> clock-alignment + no-lookahead
audit -> upsert raw_market_events + normalized_events. Returns an IngestResult the
CLI renders (per-family counts, per-adapter coverage, alignment report).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Iterable

from ..adapters.base import Adapter, IngestContext, NormalizedEvent, RawEvent
from ..adapters.registry import build_adapters
from ..config import Config
from ..storage import connect
from .audit import SourceAlignment, alignment_report, lookahead_violations
from .clock import UTC


@dataclass
class AdapterStats:
    name: str
    source: str
    fetched: int = 0
    normalized: int = 0
    failures: int = 0       # per-item fetch failures (e.g. per-symbol RSS errors)
    error: str | None = None


@dataclass
class IngestResult:
    trade_date: dt.date
    universe_size: int
    total_normalized: int
    family_counts: dict[str, int] = field(default_factory=dict)
    adapter_stats: list[AdapterStats] = field(default_factory=list)
    alignment: list[SourceAlignment] = field(default_factory=list)
    lookahead_violations: int = 0


def load_context(cfg: Config, trade_date: dt.date, now: dt.datetime, selected_block) -> IngestContext:
    con = connect(cfg.duckdb_path)
    rows = con.execute(
        "SELECT symbol, name, sector, index_membership FROM universe ORDER BY symbol"
    ).fetchall()
    con.close()
    universe = tuple(
        {"symbol": r[0], "name": r[1], "sector": r[2], "index_membership": r[3]} for r in rows
    )
    etfs = _load_etfs(cfg)
    return IngestContext(
        trade_date=trade_date,
        now=now,
        universe=universe,
        etfs=etfs,
        settings=selected_block or {},
    )


def _load_etfs(cfg: Config) -> tuple[dict[str, str], ...]:
    import csv

    path = cfg.index_etf_file
    if not path.exists():
        return ()
    with path.open() as f:
        return tuple(dict(r) for r in csv.DictReader(f))


def run_ingest(
    cfg: Config,
    trade_date: dt.date,
    selected: Iterable[str] | None = None,
    now: dt.datetime | None = None,
    write: bool = True,
) -> IngestResult:
    now = now or dt.datetime.now(UTC)
    adapters = build_adapters(cfg.raw.get("adapters", {}), selected)

    base_ctx = load_context(cfg, trade_date, now, None)

    all_raw: list[RawEvent] = []
    by_id: dict[str, NormalizedEvent] = {}
    stats: list[AdapterStats] = []

    for adapter in adapters:
        ctx = _ctx_for(base_ctx, cfg, adapter)
        st = AdapterStats(name=adapter.name, source=adapter.source)
        try:
            raws, events = adapter.run(ctx)
            st.fetched = len(raws)
            st.normalized = len(events)
            st.failures = int(getattr(adapter, "fetch_failures", 0) or 0)
            all_raw.extend(raws)
            for e in events:
                by_id[e.event_id] = e  # last-writer-wins dedup on stable id
        except Exception as exc:  # an adapter failure must not abort the run
            st.error = f"{type(exc).__name__}: {exc}"
        stats.append(st)

    events = _resolve_precedence(list(by_id.values()))
    align = alignment_report(events)
    violations = lookahead_violations(events)

    if write:
        _write(cfg, all_raw, events)

    family_counts: dict[str, int] = {}
    for e in events:
        family_counts[e.family] = family_counts.get(e.family, 0) + 1

    return IngestResult(
        trade_date=trade_date,
        universe_size=len(base_ctx.universe),
        total_normalized=len(events),
        family_counts=dict(sorted(family_counts.items())),
        adapter_stats=stats,
        alignment=align,
        lookahead_violations=len(violations),
    )


# For overlapping bar data, prefer the higher-precedence source (data quality), recording
# data_source on every row so provenance is auditable. NOT the same as adapter run-order.
_BAR_FAMILIES = {"price_volume", "sector_peer", "macro"}
_SOURCE_PRECEDENCE = {"massive": 2, "yfinance": 1}


def _resolve_precedence(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
    best: dict[tuple, NormalizedEvent] = {}
    passthrough: list[NormalizedEvent] = []
    for e in events:
        if e.family not in _BAR_FAMILIES:
            passthrough.append(e)
            continue
        key = (e.ticker, e.event_type, e.as_storage_row()["event_time"])
        cur = best.get(key)
        if cur is None or _SOURCE_PRECEDENCE.get(e.source, 0) > _SOURCE_PRECEDENCE.get(cur.source, 0):
            best[key] = e
    return passthrough + list(best.values())


def _ctx_for(base: IngestContext, cfg: Config, adapter: Adapter) -> IngestContext:
    from ..adapters.base import replace

    block = (cfg.raw.get("adapters", {}) or {}).get(adapter.name, {}) or {}
    adapter.settings = block  # adapter reads its own config block
    return replace(base, settings=block)


def _write(cfg: Config, raws: list[RawEvent], events: list[NormalizedEvent]) -> None:
    con = connect(cfg.duckdb_path)
    try:
        if raws:
            con.executemany(
                "INSERT OR REPLACE INTO raw_market_events "
                "(event_id, ingest_time, source, ticker, payload) VALUES (?,?,?,?,?)",
                [
                    (
                        r.raw_id,
                        r.ingest_time.astimezone(UTC).replace(tzinfo=None),
                        r.source,
                        r.ticker,
                        json.dumps(r.payload, default=str),
                    )
                    for r in raws
                ],
            )
        if events:
            con.executemany(
                "INSERT OR REPLACE INTO normalized_events "
                "(event_id, event_time, ingest_time, ticker, event_type, family, source, "
                " confidence, sector, related_symbols, parent_event_id, data_source, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [_storage_tuple(e) for e in events],
            )
            _mirror_side_tables(con, events)
    finally:
        con.close()


def _mirror_side_tables(con, events: list[NormalizedEvent]) -> None:
    """Mirror typed events into their side-tables (news_events, filing_events)."""
    news = [e for e in events if e.family == "news"]
    if news:
        con.executemany(
            "INSERT OR REPLACE INTO news_events (event_id, event_time, ingest_time, ticker, "
            "headline, source, sentiment, topic) VALUES (?,?,?,?,?,?,?,?)",
            [(e.event_id, e.as_storage_row()["event_time"], e.as_storage_row()["ingest_time"],
              e.ticker, (e.payload or {}).get("headline"), e.source, None, None) for e in news],
        )
    filings = [e for e in events if e.family == "filing"]
    if filings:
        con.executemany(
            "INSERT OR REPLACE INTO filing_events (event_id, event_time, ticker, form_type, "
            "accession, url) VALUES (?,?,?,?,?,?)",
            [(e.event_id, e.as_storage_row()["event_time"], e.ticker,
              (e.payload or {}).get("form_type"), (e.payload or {}).get("accession"),
              (e.payload or {}).get("url")) for e in filings],
        )
    flow = [e for e in events if e.family == "equity_flow"]
    if flow:
        # mirror into equity_flow_state — the L1 baseline source (idempotent on ticker+ts)
        for e in flow:
            row = e.as_storage_row()
            p = e.payload or {}
            con.execute(
                "DELETE FROM equity_flow_state WHERE ticker=? AND ts=? AND "
                "((? IS NOT NULL AND short_pct IS NOT NULL) OR (? IS NOT NULL AND off_exchange_share IS NOT NULL))",
                [e.ticker, row["event_time"], p.get("short_pct"), p.get("off_exchange_share")])
            con.execute(
                "INSERT INTO equity_flow_state (ticker, ts, short_pct, off_exchange_share, data_source) "
                "VALUES (?,?,?,?,?)",
                [e.ticker, row["event_time"], p.get("short_pct"), p.get("off_exchange_share"), e.source])


def _storage_tuple(e: NormalizedEvent):
    row = e.as_storage_row()
    return (
        row["event_id"],
        row["event_time"],
        row["ingest_time"],
        row["ticker"],
        row["event_type"],
        row["family"],
        row["source"],
        row["confidence"],
        row["sector"],
        row["related_symbols"],
        row["parent_event_id"],
        row["data_source"],
        json.dumps(row["payload"], default=str),
    )
