"""FRED macro adapter: yields, rates, and macro prints (ROADMAP §6 macro driver).

Free-first: uses FRED's keyless `fredgraph.csv` endpoint so no API key is required.
If `api_key_env` resolves to a key, the official fredapi path is used instead.
Emits one `MacroPrint` event per series per observation on the trade date.
"""
from __future__ import annotations

import datetime as dt
import io
import os

from .base import Adapter, IngestContext, RawEvent, NormalizedEvent
from ..ingest.clock import market_close_utc

# Default macro series (daily). All free. Override via config adapters.fred.series.
DEFAULT_SERIES = {
    "DGS10": "10Y Treasury yield",
    "DGS2": "2Y Treasury yield",
    "T10Y2Y": "10Y-2Y spread",
    "DFF": "Effective fed funds rate",
    "VIXCLS": "CBOE VIX close",
    "DTWEXBGS": "Broad dollar index",
}
_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


class FredAdapter(Adapter):
    name = "fred"
    source = "fred"
    default_family = "macro"
    reliability = 0.97
    # FRED daily series publish ~1 business day after the observation date.
    expected_latency_seconds = 36 * 3600.0
    priority = 1

    def _series(self) -> dict[str, str]:
        cfg = self.settings.get("series")
        if isinstance(cfg, dict) and cfg:
            return cfg
        if isinstance(cfg, list) and cfg:
            return {s: s for s in cfg}
        return DEFAULT_SERIES

    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        out: list[RawEvent] = []
        for series_id, desc in self._series().items():
            obs = self._observation(series_id, ctx.trade_date)
            if obs is None:
                continue
            obs_date, value = obs
            out.append(
                RawEvent(
                    self.source,
                    ctx.now,
                    None,
                    {
                        "series_id": series_id,
                        "description": desc,
                        "value": value,
                        # as-of: the real observation date known by trade_date (publish lag)
                        "observation_date": obs_date.isoformat(),
                    },
                )
            )
        return out

    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        obs_date = dt.date.fromisoformat(raw.payload["observation_date"])
        return [
            self._event(
                event_type="MacroPrint",
                event_time=market_close_utc(obs_date),  # reference-day value
                ingest_time=raw.ingest_time,
                ticker=raw.payload["series_id"],  # series id used as the symbol
                payload={
                    "series_id": raw.payload["series_id"],
                    "description": raw.payload["description"],
                    "value": raw.payload["value"],
                },
                id_extra=raw.payload["series_id"],
            )
        ]

    def _lookback_days(self) -> int:
        return int(self.settings.get("lookback_days", 14))

    # --- network ---------------------------------------------------------------
    def _observation(self, series_id: str, on: dt.date) -> tuple[dt.date, float] | None:
        """Latest (date, value) on or before `on` — the value known by trade_date.
        Honours FRED publication lag (daily series post ~1 business day late) while
        staying point-in-time correct (observation_date <= trade_date, no lookahead)."""
        key_env = self.settings.get("api_key_env", "FRED_API_KEY")
        api_key = os.environ.get(key_env) if key_env else None
        start = on - dt.timedelta(days=self._lookback_days())
        try:
            if api_key:
                return self._via_fredapi(series_id, start, on, api_key)
            return self._via_csv(series_id, start, on)
        except Exception:
            return None

    @staticmethod
    def _via_csv(series_id: str, start: dt.date, on: dt.date) -> tuple[dt.date, float] | None:
        import csv

        import requests

        params = {"id": series_id, "cosd": start.isoformat(), "coed": on.isoformat()}
        r = requests.get(_CSV_URL, params=params, timeout=20)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))[1:]  # skip header
        for row in reversed(rows):  # newest valid observation <= on
            if len(row) >= 2 and row[1] not in (".", ""):
                d = dt.date.fromisoformat(row[0])
                if d <= on:
                    return d, float(row[1])
        return None

    @staticmethod
    def _via_fredapi(
        series_id: str, start: dt.date, on: dt.date, api_key: str
    ) -> tuple[dt.date, float] | None:
        from fredapi import Fred

        fred = Fred(api_key=api_key)
        s = fred.get_series(series_id, observation_start=start, observation_end=on)
        for idx, val in reversed(list(s.items())):
            d = idx.date() if hasattr(idx, "date") else idx
            if val == val and d <= on:  # not NaN
                return d, float(val)
        return None
