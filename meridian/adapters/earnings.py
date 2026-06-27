"""Earnings adapter (ROADMAP §6 earnings driver, free-first via yfinance).

Emits `EarningsResult`/`EarningsAnnouncement` events for universe names whose
earnings date falls on the trade date. yfinance has no bulk earnings calendar, so
this scans per ticker (bounded by `max_symbols`, concurrent) — the production bulk
path is Robinhood MCP `get_earnings_calendar` (priority 2). Coverage scanned is
reported honestly by the pipeline.
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .base import Adapter, IngestContext, RawEvent, NormalizedEvent
from ..ingest.clock import to_utc, market_time_utc, MARKET_CLOSE_LOCAL


class EarningsAdapter(Adapter):
    name = "earnings"
    source = "earnings_yf"
    default_family = "earnings"
    reliability = 0.90
    expected_latency_seconds = 0.0
    priority = 1

    def _max_symbols(self) -> int:
        return int(self.settings.get("max_symbols", 60))

    def _workers(self) -> int:
        return int(self.settings.get("workers", 8))

    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        symbols = list(ctx.universe_symbols)[: self._max_symbols()]

        def one(sym: str) -> RawEvent | None:
            rec = self._earnings_on(sym, ctx.trade_date)
            if rec is None:
                return None
            return RawEvent(self.source, ctx.now, sym, rec)

        out: list[RawEvent] = []
        with ThreadPoolExecutor(max_workers=self._workers()) as pool:
            for raw in pool.map(one, symbols):
                if raw is not None:
                    out.append(raw)
        return out

    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        reported = raw.payload.get("reported_eps")
        event_type = "EarningsResult" if reported is not None else "EarningsAnnouncement"
        when = (
            dt.datetime.fromisoformat(raw.payload["event_time_iso"])
            if raw.payload.get("event_time_iso")
            else market_time_utc(dt.date.fromisoformat(raw.payload["date"]), MARKET_CLOSE_LOCAL)
        )
        return [
            self._event(
                event_type=event_type,
                event_time=when,
                ingest_time=raw.ingest_time,
                ticker=raw.ticker,
                sector=ctx.sector_of(raw.ticker),
                payload={
                    "eps_estimate": raw.payload.get("eps_estimate"),
                    "reported_eps": reported,
                    "surprise_pct": raw.payload.get("surprise_pct"),
                },
            )
        ]

    # --- network ---------------------------------------------------------------
    def _earnings_on(self, symbol: str, on: dt.date) -> dict[str, Any] | None:
        import logging
        import math

        import yfinance as yf

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # quiet "delisted" noise
        try:
            df = yf.Ticker(symbol).get_earnings_dates(limit=24)
        except Exception:
            return None
        if df is None or len(df) == 0:
            return None

        def clean(v: Any) -> Any:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return None
            return float(v)

        for idx, row in df.iterrows():
            announce = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else None
            if announce is None:
                continue
            # Compare on the exchange-local calendar date of the announcement.
            local_date = announce.date() if announce.tzinfo is None else announce.astimezone(announce.tzinfo).date()
            if local_date != on:
                continue
            when = to_utc(announce)
            return {
                "date": on.isoformat(),
                "event_time_iso": when.isoformat() if when else None,
                "eps_estimate": clean(row.get("EPS Estimate")),
                "reported_eps": clean(row.get("Reported EPS")),
                "surprise_pct": clean(row.get("Surprise(%)")),
            }
        return None
