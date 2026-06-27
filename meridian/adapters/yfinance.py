"""yfinance adapter: daily OHLCV bars for stocks, sector/index ETFs, and macro indices.

One `DailyBar`/`ETFBar`/`MacroQuote` event per symbol per trade date. NO derived
signals (PriceMove, GapUp, RelVolumeSpike, ...) and NO thresholds — those are
Layer-1 featurization (Phase 2). We only record the objective bar.

Family mapping:
  - universe stocks         -> price_volume   (DailyBar)
  - ETFs role index/sector/theme -> sector_peer (ETFBar)
  - ETFs/indices role macro -> macro          (MacroQuote)
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter, IngestContext, RawEvent, NormalizedEvent
from ..ingest.clock import market_close_utc

_CHUNK = 100  # symbols per yfinance batch request


def _role_to_family(role: str) -> str:
    return "macro" if role == "macro" else "sector_peer"


def _role_to_event_type(role: str) -> str:
    return "MacroQuote" if role == "macro" else "ETFBar"


class YFinanceAdapter(Adapter):
    name = "yfinance"
    source = "yfinance"
    default_family = "price_volume"
    reliability = 0.95
    expected_latency_seconds = 0.0  # EOD bars: event_time is the session close
    priority = 1

    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        # Targets: stocks (role=stock) + configured ETFs/indices (role from csv).
        targets: dict[str, str] = {s: "stock" for s in ctx.universe_symbols}
        for row in ctx.etfs:
            targets[row["symbol"]] = row.get("role", "sector")

        bars = self._download(list(targets), ctx.trade_date)
        out: list[RawEvent] = []
        for symbol, bar in bars.items():
            payload = {**bar, "role": targets.get(symbol, "stock"), "trade_date": ctx.trade_date.isoformat()}
            out.append(RawEvent(self.source, ctx.now, symbol, payload))
        return out

    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        role = raw.payload.get("role", "stock")
        trade_date = dt.date.fromisoformat(raw.payload["trade_date"])
        event_time = market_close_utc(trade_date)  # bar settles at the session close
        if role == "stock":
            family, event_type, sector = "price_volume", "DailyBar", ctx.sector_of(raw.ticker)
        else:
            family, event_type, sector = _role_to_family(role), _role_to_event_type(role), None
        payload = {k: raw.payload[k] for k in ("open", "high", "low", "close", "volume") if k in raw.payload}
        payload["role"] = role
        return [
            self._event(
                event_type=event_type,
                event_time=event_time,
                ingest_time=raw.ingest_time,
                ticker=raw.ticker,
                family=family,
                sector=sector,
                payload=payload,
            )
        ]

    # --- network ---------------------------------------------------------------
    def _download(self, symbols: list[str], trade_date: dt.date) -> dict[str, dict[str, Any]]:
        """Return {symbol: {open,high,low,close,volume}} for `trade_date`. Resilient
        to per-symbol gaps and feed errors (returns whatever resolved)."""
        import yfinance as yf

        start = trade_date
        end = trade_date + dt.timedelta(days=1)
        result: dict[str, dict[str, Any]] = {}
        for i in range(0, len(symbols), _CHUNK):
            chunk = symbols[i : i + _CHUNK]
            try:
                df = yf.download(
                    chunk,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    auto_adjust=False,
                    progress=False,
                    threads=True,
                    group_by="ticker",
                )
            except Exception:
                continue
            result.update(self._extract(df, chunk, trade_date))
        return result

    @staticmethod
    def _extract(df, chunk: list[str], trade_date: dt.date) -> dict[str, dict[str, Any]]:
        import math

        out: dict[str, dict[str, Any]] = {}
        if df is None or len(df) == 0:
            return out
        single = len(chunk) == 1

        def row_for(symbol: str) -> dict[str, Any] | None:
            try:
                sub = df if single else df[symbol]
            except Exception:
                return None
            sub = sub.dropna(how="all")
            if len(sub) == 0:
                return None
            row = sub.iloc[-1]  # the target date's bar
            vals = {
                "open": row.get("Open"),
                "high": row.get("High"),
                "low": row.get("Low"),
                "close": row.get("Close"),
                "volume": row.get("Volume"),
            }
            if vals["close"] is None or (isinstance(vals["close"], float) and math.isnan(vals["close"])):
                return None
            return {k: (float(v) if k != "volume" else int(v)) for k, v in vals.items() if v is not None}

        for symbol in chunk:
            bar = row_for(symbol)
            if bar is not None:
                out[symbol] = bar
        return out
