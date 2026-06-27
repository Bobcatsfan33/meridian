"""Trailing price/macro history fetch for the state builder (network).

Returns a PriceWindow = {symbol: [bar, ...]} sorted ascending by date, where bar is
{date, open, high, low, close, volume}. yfinance supplies stock/ETF/index bars; FRED
supplies macro series (close=value). Kept separate from featurization so the pure
state math stays deterministic and golden-testable with synthetic windows.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

Bar = dict[str, Any]
PriceWindow = dict[str, list[Bar]]

_CHUNK = 100


def fetch_yf_window(symbols: list[str], start: dt.date, end: dt.date) -> PriceWindow:
    """Daily OHLCV for `symbols` over [start, end]. Resilient to per-symbol gaps."""
    import yfinance as yf

    out: PriceWindow = {}
    for i in range(0, len(symbols), _CHUNK):
        chunk = symbols[i : i + _CHUNK]
        try:
            df = yf.download(
                chunk,
                start=start.isoformat(),
                end=(end + dt.timedelta(days=1)).isoformat(),
                auto_adjust=False,
                progress=False,
                threads=True,
                group_by="ticker",
            )
        except Exception:
            continue
        out.update(_extract(df, chunk))
    return out


def fetch_fred_window(
    series: dict[str, str], start: dt.date, end: dt.date, settings: dict | None = None
) -> PriceWindow:
    """Daily macro values for FRED `series` (id->desc) as single-value bars."""
    from ..adapters.fred import FredAdapter

    adapter = FredAdapter(settings or {})
    out: PriceWindow = {}
    for series_id in series:
        bars = _fred_series_bars(adapter, series_id, start, end)
        if bars:
            out[series_id] = bars
    return out


def _fred_series_bars(adapter, series_id: str, start: dt.date, end: dt.date) -> list[Bar]:
    import csv
    import io

    import requests

    try:
        r = requests.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv",
            params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()},
            timeout=20,
        )
        r.raise_for_status()
    except Exception:
        return []
    bars: list[Bar] = []
    for row in list(csv.reader(io.StringIO(r.text)))[1:]:
        if len(row) >= 2 and row[1] not in (".", ""):
            try:
                d = dt.date.fromisoformat(row[0])
                v = float(row[1])
            except ValueError:
                continue
            bars.append({"date": d, "open": v, "high": v, "low": v, "close": v, "volume": None})
    bars.sort(key=lambda b: b["date"])
    return bars


def _extract(df, chunk: list[str]) -> PriceWindow:
    import math

    out: PriceWindow = {}
    if df is None or len(df) == 0:
        return out
    single = len(chunk) == 1
    for symbol in chunk:
        try:
            sub = df if single else df[symbol]
        except Exception:
            continue
        sub = sub.dropna(how="all")
        bars: list[Bar] = []
        for idx, row in sub.iterrows():
            close = row.get("Close")
            if close is None or (isinstance(close, float) and math.isnan(close)):
                continue
            d = idx.date() if hasattr(idx, "date") else idx
            vol = row.get("Volume")
            bars.append(
                {
                    "date": d,
                    "open": _f(row.get("Open")),
                    "high": _f(row.get("High")),
                    "low": _f(row.get("Low")),
                    "close": _f(close),
                    "volume": int(vol) if vol is not None and not _isnan(vol) else None,
                }
            )
        if bars:
            bars.sort(key=lambda b: b["date"])
            out[symbol] = bars
    return out


def _f(v) -> float | None:
    if v is None or _isnan(v):
        return None
    return float(v)


def _isnan(v) -> bool:
    return isinstance(v, float) and v != v
