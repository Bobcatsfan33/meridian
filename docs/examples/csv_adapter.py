"""Worked example: a REAL Meridian adapter over a simple CSV — copy this for your format.

CSV schema (header required):
    event_time,ticker,event_type,family,value,payload_json
  - event_time   ISO-8601, ideally UTC (e.g. 2026-06-26T20:00:00+00:00). Naive -> assumed UTC.
                 THIS IS THE NO-LOOKAHEAD ANCHOR — it must be when the event HAPPENED, not now.
  - ticker       symbol the event pertains to (may be blank for market-wide rows).
  - event_type   free-form, be consistent (DailyBar, HeadlineHit, Filing8K, ShortVolumeSpike, ...).
  - family       one of the canonical families (price_volume, news, filing, equity_flow, ...).
  - value        a single numeric measure for the event (e.g. close, short_pct). May be blank.
  - payload_json JSON object of extra typed fields (e.g. {"close":283.78,"volume":2.6e8}). May be blank.

fetch() reads the CSV (path from this adapter's config block, settings["csv_path"]).
normalize() is PURE: one CSV row -> one canonical NormalizedEvent, deterministic and golden-testable.

To use: copy into meridian/adapters/, register it in meridian/adapters/registry.py, add a config
block adapters.my_csv: {enabled: true, csv_path: /path/to/your.csv}, then
`meridian ingest --date <D> -a my_csv`.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
from typing import Any

from meridian.adapters.base import Adapter, IngestContext, NormalizedEvent, RawEvent
from meridian.ingest.clock import to_utc


class CsvAdapter(Adapter):
    name = "csv_example"
    source = "csv_example"
    default_family = "price_volume"
    reliability = 0.90          # data-quality of your source, NOT a signal threshold
    expected_latency_seconds = 0.0
    priority = 1

    def _csv_path(self) -> pathlib.Path | None:
        p = self.settings.get("csv_path")
        return pathlib.Path(p) if p else None

    # --- IO (not unit-tested) --------------------------------------------------
    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        """Read the CSV; keep rows whose event_time falls on ctx.trade_date. Never fatal."""
        path = self._csv_path()
        if not path or not path.exists():
            self.fetch_failures = 1
            return []
        out: list[RawEvent] = []
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                ev_time = _parse_dt(row.get("event_time"))
                if ev_time is None or ev_time.date() != ctx.trade_date:
                    continue
                out.append(RawEvent(self.source, ctx.now, (row.get("ticker") or "").strip() or None, dict(row)))
        return out

    # --- PURE transform (golden-tested) ---------------------------------------
    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        r = raw.payload
        ev_time = _parse_dt(r.get("event_time"))
        family = (r.get("family") or self.default_family).strip()
        event_type = (r.get("event_type") or "Event").strip()
        if ev_time is None:
            return []
        payload: dict[str, Any] = {}
        if (r.get("payload_json") or "").strip():
            try:
                payload.update(json.loads(r["payload_json"]))
            except (ValueError, TypeError):
                pass
        if (r.get("value") or "").strip():
            try:
                payload["value"] = float(r["value"])
            except ValueError:
                pass
        # _event builds a stable event_id (dedup/idempotency) + UTC times + family validation
        return [
            self._event(
                event_type=event_type,
                event_time=ev_time,                 # real event time (no-lookahead anchor)
                ingest_time=raw.ingest_time,        # = ctx.now
                ticker=raw.ticker,
                family=family,
                sector=ctx.sector_of(raw.ticker) if raw.ticker else None,
                payload=payload,
            )
        ]


def _parse_dt(s: str | None) -> dt.datetime | None:
    if not s or not s.strip():
        return None
    try:
        return to_utc(dt.datetime.fromisoformat(s.strip()))   # naive -> assumed UTC
    except ValueError:
        return None
