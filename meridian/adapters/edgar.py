"""SEC EDGAR filings adapter (free, authoritative; ROADMAP §6 filings driver).

Pulls the EDGAR daily index for the trade date, keeps filings whose CIK maps to a
universe ticker, and emits one `Filing<FORM>` event each. CIK<->ticker comes from
SEC's free company_tickers.json. Forms of interest are structural catalysts
(8-K, S-3, SC 13D/G, Form 4, 10-Q/K) per ROADMAP §7.

The daily index gives date-level granularity only; event_time is set to the close
and `time_precision="day"` is recorded so the clock-alignment audit treats the
day-level stamp honestly (its tolerance band covers same-day acceptance).
"""
from __future__ import annotations

import datetime as dt

from .base import Adapter, IngestContext, RawEvent, NormalizedEvent
from ..ingest.clock import market_close_utc

FORMS_OF_INTEREST = {
    "8-K": "Filing8K",
    "S-3": "FilingS3",
    "S-3/A": "FilingS3",
    "SC 13D": "Filing13D",
    "SC 13D/A": "Filing13D",
    "SC 13G": "Filing13G",
    "SC 13G/A": "Filing13G",
    "4": "Form4Insider",
    "10-Q": "Filing10Q",
    "10-K": "Filing10K",
    "424B5": "ShelfTakedown",
}
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class EdgarAdapter(Adapter):
    name = "edgar"
    source = "sec_edgar"
    default_family = "filing"
    reliability = 0.99
    expected_latency_seconds = 8 * 3600.0  # day-level index; same-day acceptance
    priority = 1

    def _user_agent(self) -> str:
        return self.settings.get("user_agent") or "meridian research contact@example.com"

    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        cik_to_ticker = self._cik_map(set(ctx.universe_symbols))
        if not cik_to_ticker:
            return []
        rows = self._daily_index(ctx.trade_date)
        out: list[RawEvent] = []
        for form, company, cik, date_filed, fname in rows:
            mapped = FORMS_OF_INTEREST.get(form.strip())
            ticker = cik_to_ticker.get(int(cik)) if cik.isdigit() else None
            if not mapped or not ticker:
                continue
            out.append(
                RawEvent(
                    self.source,
                    ctx.now,
                    ticker,
                    {
                        "form_type": form.strip(),
                        "event_type": mapped,
                        "company": company.strip(),
                        "cik": int(cik),
                        "date_filed": date_filed.strip(),
                        "url": f"https://www.sec.gov/Archives/{fname.strip()}",
                    },
                )
            )
        return out

    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        date_filed = dt.date.fromisoformat(raw.payload["date_filed"])
        return [
            self._event(
                event_type=raw.payload["event_type"],
                event_time=market_close_utc(date_filed),
                ingest_time=raw.ingest_time,
                ticker=raw.ticker,
                sector=ctx.sector_of(raw.ticker),
                payload={
                    "form_type": raw.payload["form_type"],
                    "company": raw.payload["company"],
                    "accession": raw.payload["url"].rsplit("/", 1)[-1],
                    "url": raw.payload["url"],
                    "time_precision": "day",
                },
                id_extra=raw.payload["url"],
            )
        ]

    # --- network ---------------------------------------------------------------
    def _cik_map(self, wanted: set[str]) -> dict[int, str]:
        import requests

        try:
            r = requests.get(_TICKERS_URL, headers={"User-Agent": self._user_agent()}, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return {}
        out: dict[int, str] = {}
        for rec in data.values():
            tkr = str(rec.get("ticker", "")).upper()
            if tkr in wanted:
                out[int(rec["cik_str"])] = tkr
        return out

    def _daily_index(self, on: dt.date) -> list[tuple[str, str, str, str, str]]:
        import requests

        qtr = (on.month - 1) // 3 + 1
        url = (
            f"https://www.sec.gov/Archives/edgar/daily-index/{on.year}/QTR{qtr}/"
            f"form.{on.strftime('%Y%m%d')}.idx"
        )
        try:
            r = requests.get(url, headers={"User-Agent": self._user_agent()}, timeout=30)
            if r.status_code != 200:
                return []
            return self._parse_form_idx(r.text)
        except Exception:
            return []

    @staticmethod
    def _parse_form_idx(text: str) -> list[tuple[str, str, str, str, str]]:
        """Parse form.idx data rows. Each row ends with: CIK  DateFiled(YYYYMMDD)  FileName.
        The head before those three tokens is `FormType  CompanyName` (2+ spaces apart).
        Returns (form_type, company, cik, date_iso, filename)."""
        import re

        lines = text.splitlines()
        start = 0
        for i, ln in enumerate(lines):
            if set(ln.strip()) == {"-"} and len(ln.strip()) > 5:
                start = i + 1
                break
        rows: list[tuple[str, str, str, str, str]] = []
        for ln in lines[start:]:
            if not ln.strip():
                continue
            parts = ln.rsplit(None, 3)  # [head, cik, YYYYMMDD, filename]
            if len(parts) != 4:
                continue
            head, cik, date_raw, fname = parts
            if not (cik.isdigit() and re.fullmatch(r"\d{8}", date_raw)):
                continue
            fc = re.split(r"\s{2,}", head.strip(), maxsplit=1)
            form = fc[0].strip()
            company = fc[1].strip() if len(fc) > 1 else ""
            date_iso = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}"
            rows.append((form, company, cik, date_iso, fname))
        return rows
