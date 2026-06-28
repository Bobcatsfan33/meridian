"""FINRA equity-flow adapter (Part B) — FREE, no key, no rate limit.

Two published FINRA datasets (delayed/aggregate — the robust dark-pool baseline):
  * Reg SHO Daily Short Sale Volume (per-symbol short vs total volume), one file/day:
      https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
  * Weekly ATS (dark-pool) Transparency aggregate share volume, per symbol (POST filter).

Emits family="equity_flow": ShortVolumeSpike (short% measure) and DarkPoolAccumulation
(off-exchange share measure). NO thresholds here — abnormality is graded in L1 vs the
name's OWN trailing baseline. Dual timestamps: event_time = the data's as-of date close.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter, IngestContext, NormalizedEvent, RawEvent
from ..ingest.clock import market_close_utc

REGSHO_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"
ATS_URL = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"
_UA = {"User-Agent": "meridian research contact@example.com"}


class FinraAdapter(Adapter):
    name = "finra"
    source = "finra"
    default_family = "equity_flow"
    reliability = 0.92
    expected_latency_seconds = 36 * 3600.0   # Reg SHO ~next business day; ATS weeks delayed
    priority = 1

    def _ats_max_symbols(self) -> int:
        return int(self.settings.get("ats_max_symbols", 40))

    def _ats_watchlist(self) -> list[str]:
        wl = self.settings.get("ats_watchlist")
        return wl if isinstance(wl, list) else []

    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        self.fetch_failures = 0
        out: list[RawEvent] = []
        universe = set(ctx.universe_symbols)

        text = self._get_regsho(ctx.trade_date)
        if text:
            for rec in self._parse_regsho(text, universe):
                out.append(RawEvent(self.source, ctx.now, rec["symbol"],
                                    {**rec, "kind": "short_volume"}))

        # ATS dark-pool: bounded per-symbol (no rate limit, but be polite).
        wl = self._ats_watchlist() or list(universe)
        for sym in wl[: self._ats_max_symbols()]:
            rec = self._get_ats(sym)
            if rec is None:
                self.fetch_failures += 1
                continue
            out.append(RawEvent(self.source, ctx.now, sym, {**rec, "kind": "dark_pool"}))
        return out

    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        kind = raw.payload["kind"]
        sector = ctx.sector_of(raw.ticker)
        if kind == "short_volume":
            as_of = dt.date.fromisoformat(raw.payload["as_of"])
            return [self._event(
                event_type="ShortVolumeSpike", event_time=market_close_utc(as_of),
                ingest_time=raw.ingest_time, ticker=raw.ticker, sector=sector,
                payload={"short_volume": raw.payload["short_volume"],
                         "total_volume": raw.payload["total_volume"],
                         "short_pct": raw.payload["short_pct"]},
                id_extra="shortvol")]
        as_of = dt.date.fromisoformat(raw.payload["week_start"])
        return [self._event(
            event_type="DarkPoolAccumulation", event_time=market_close_utc(as_of),
            ingest_time=raw.ingest_time, ticker=raw.ticker, sector=sector,
            payload={"off_exchange_share": raw.payload["ats_shares"], "week_start": raw.payload["week_start"]},
            id_extra="darkpool")]

    # --- parsing (pure; golden-tested) ----------------------------------------
    @staticmethod
    def _parse_regsho(text: str, universe: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        lines = text.splitlines()
        if not lines:
            return out
        for ln in lines[1:]:  # skip header
            parts = ln.split("|")
            if len(parts) < 5 or parts[1] not in universe:
                continue
            try:
                short_v = float(parts[2])
                total_v = float(parts[4])
            except ValueError:
                continue
            if total_v <= 0:
                continue
            out.append({"symbol": parts[1], "as_of": _iso(parts[0]),
                        "short_volume": short_v, "total_volume": total_v,
                        "short_pct": short_v / total_v})
        return out

    @staticmethod
    def _aggregate_ats(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Sum weekly ATS share quantity for the latest week present in `rows`."""
        if not rows:
            return None
        latest = max(r.get("summaryStartDate", "") for r in rows)
        total = sum(float(r.get("totalWeeklyShareQuantity") or 0)
                    for r in rows if r.get("summaryStartDate") == latest)
        if not latest or total <= 0:
            return None
        return {"week_start": latest, "ats_shares": total}

    # --- network ---------------------------------------------------------------
    def _get_regsho(self, on: dt.date) -> str | None:
        import requests

        try:
            r = requests.get(REGSHO_URL.format(ymd=on.strftime("%Y%m%d")), headers=_UA, timeout=30)
            return r.text if r.status_code == 200 else None
        except Exception:
            return None

    def _get_ats(self, symbol: str) -> dict[str, Any] | None:
        import csv
        import io

        import requests

        body = {"compareFilters": [{"compareType": "EQUAL",
                                    "fieldName": "issueSymbolIdentifier", "fieldValue": symbol}],
                "limit": 200}
        try:
            r = requests.post(ATS_URL, json=body, headers={**_UA, "Accept": "text/plain"}, timeout=25)
            if r.status_code != 200:
                return None
            rows = list(csv.DictReader(io.StringIO(r.text)))
        except Exception:
            return None
        return self._aggregate_ats(rows)


def _iso(ymd: str) -> str:
    ymd = ymd.strip()
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
