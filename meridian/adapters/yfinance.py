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
from ..ingest.clock import market_close_utc, to_utc

_CHUNK = 100  # symbols per yfinance batch request
_INTERVAL_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60, "90m": 90}


def interval_minutes(interval: str) -> int:
    return _INTERVAL_MINUTES.get(interval, 5)


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
        payload["data_source"] = "yfinance"   # provenance (precedence: massive > yfinance)
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

    # --- intraday (Phase 7 / Step A2) ------------------------------------------
    def download_intraday(self, symbols: list[str], date: dt.date,
                          interval: str = "5m") -> dict[str, list[dict[str, Any]]]:
        """Return {symbol: [bar...]} of intraday bars for `date`. Each bar is
        {start (tz-aware), open, high, low, close, volume}. Resilient to gaps."""
        import yfinance as yf

        end = date + dt.timedelta(days=1)
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(symbols), _CHUNK):
            chunk = symbols[i : i + _CHUNK]
            try:
                df = yf.download(
                    chunk, start=date.isoformat(), end=end.isoformat(), interval=interval,
                    auto_adjust=False, progress=False, threads=True, group_by="ticker",
                )
            except Exception:
                continue
            out.update(self._intraday_extract(df, chunk))
        return out

    @staticmethod
    def _intraday_extract(df, chunk: list[str]) -> dict[str, list[dict[str, Any]]]:
        import math

        out: dict[str, list[dict[str, Any]]] = {}
        if df is None or len(df) == 0:
            return out
        single = len(chunk) == 1
        for symbol in chunk:
            try:
                sub = df if single else df[symbol]
            except Exception:
                continue
            bars: list[dict[str, Any]] = []
            for idx, row in sub.dropna(how="all").iterrows():
                close = row.get("Close")
                if close is None or (isinstance(close, float) and math.isnan(close)):
                    continue
                start = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                bars.append({"start": start, "open": _f(row.get("Open")), "high": _f(row.get("High")),
                             "low": _f(row.get("Low")), "close": _f(close), "volume": _i(row.get("Volume"))})
            if bars:
                out[symbol] = bars
        return out


def _f(v):
    import math
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def _i(v):
    import math
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else int(v)


def intraday_state_rows(symbol: str, bars: list[dict[str, Any]], interval_min: int) -> list[tuple]:
    """Pure: intraday bars -> ticker_state_1m rows (ts = bar CLOSE time, naive UTC).

    ts = bar start + interval, converted to UTC. ret_1m is the bar-over-bar return;
    rel_volume is the bar volume vs the session's mean bar volume. No-lookahead is the
    caller's job (filter ts <= now). Deterministic; golden-tested.
    """
    bars = sorted(bars, key=lambda b: b["start"])
    vols = [b["volume"] for b in bars if b.get("volume")]
    mean_vol = (sum(vols) / len(vols)) if vols else None
    rows: list[tuple] = []
    prev_close = None
    for b in bars:
        c = b.get("close")
        if c is None:
            continue
        close_ts = to_utc(b["start"] + dt.timedelta(minutes=interval_min)).replace(tzinfo=None)
        h, lo = b.get("high"), b.get("low")
        vwap = (h + lo + c) / 3 if None not in (h, lo) else c
        ret = (c / prev_close - 1.0) if prev_close else float("nan")
        rel = (b["volume"] / mean_vol) if (mean_vol and b.get("volume")) else float("nan")
        rows.append((symbol, close_ts, c, vwap, rel, None, ret))
        prev_close = c
    return rows
