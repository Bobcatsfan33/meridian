"""News adapter — FREE Yahoo Finance per-symbol RSS (ROADMAP §6 news driver).

  https://feeds.finance.yahoo.com/rss/2.0/headline?s={SYM}&region=US&lang=en-US

Per-symbol fetch so the ticker is known precisely. pubDate -> event_time (tz-aware UTC
via ingest/clock.to_utc); ingest_time = ctx.now (dual timestamps). Emits family="news"
HeadlineHit events; the pipeline also mirrors them into the news_events side-table.

Scope (config adapters.news_rss.scope): "universe" (EOD; throttled <=N req/sec with
backoff) or "movers" (intraday; a configured watchlist). Never fatal per symbol —
per-symbol failures are counted into adapter stats. Dedup by stable content id (guid).
"""
from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any

from .base import Adapter, IngestContext, NormalizedEvent, RawEvent
from ..ingest.clock import to_utc

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "JPM", "AMD", "SPY"]
_STOP = {"A", "I", "AT", "BE", "ON", "OR", "IT", "IS", "AS", "SO", "TO", "UP", "USA",
         "CEO", "CFO", "IPO", "GDP", "FED", "ETF", "AI", "US", "EU", "Q1", "Q2", "Q3", "Q4"}


class NewsRssAdapter(Adapter):
    name = "news_rss"
    source = "yahoo_rss"
    default_family = "news"
    reliability = 0.80
    expected_latency_seconds = 15 * 60.0
    priority = 1

    def _scope(self) -> str:
        return self.settings.get("scope", "movers")

    def _watchlist(self) -> list[str]:
        wl = self.settings.get("watchlist")
        return wl if isinstance(wl, list) and wl else DEFAULT_WATCHLIST

    def _max_req_per_sec(self) -> float:
        return float(self.settings.get("max_req_per_sec", 5))

    def _symbols(self, ctx: IngestContext) -> list[str]:
        if self._scope() == "universe":
            return list(ctx.universe_symbols)
        uni = set(ctx.universe_symbols)
        return [s for s in self._watchlist() if s in uni or s == "SPY"]

    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        self.fetch_failures = 0
        delay = 1.0 / max(self._max_req_per_sec(), 0.1)
        out: list[RawEvent] = []
        for i, sym in enumerate(self._symbols(ctx)):
            if i:
                time.sleep(delay)  # throttle to <= N req/sec
            xml = self._get(YAHOO_RSS.format(sym=sym))
            if xml is None:
                self.fetch_failures += 1
                continue
            out.extend(
                r for r in self._parse_feed(xml, sym, ctx)
                if dt.datetime.fromisoformat(r.payload["published_iso"]).date() <= ctx.trade_date
            )
        return out

    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        title = raw.payload["title"]
        sym = raw.payload["query_symbol"]
        published = dt.datetime.fromisoformat(raw.payload["published_iso"])
        related = self._tag_symbols(title, ctx) or (sym,)
        return [
            self._event(
                event_type="HeadlineHit",
                event_time=published,
                ingest_time=raw.ingest_time,
                ticker=sym,
                sector=ctx.sector_of(sym),
                related_symbols=related,
                payload={
                    "headline": title,
                    "url": raw.payload.get("link", ""),
                    "guid": raw.payload.get("guid", ""),
                    "matched_symbols": list(related),
                    "source_feed": "yahoo_rss",
                },
                id_extra=raw.payload.get("guid") or raw.payload.get("link") or title,
            )
        ]

    # --- parsing (pure; golden-tested) ----------------------------------------
    @staticmethod
    def _parse_feed(xml: str, symbol: str, ctx: IngestContext) -> list[RawEvent]:
        import feedparser

        parsed = feedparser.parse(xml)
        seen: set[str] = set()
        out: list[RawEvent] = []
        for e in parsed.entries:
            published = _entry_time(e)
            title = (e.get("title") or "").strip()
            if published is None or not title:
                continue
            guid = (e.get("id") or e.get("link") or title)
            if guid in seen:  # dedup by stable content id within the feed
                continue
            seen.add(guid)
            out.append(RawEvent(
                "yahoo_rss", ctx.now, symbol,
                {"title": title, "link": e.get("link", ""), "guid": e.get("id", ""),
                 "published_iso": published.isoformat(), "query_symbol": symbol},
            ))
        return out

    # --- network ---------------------------------------------------------------
    def _get(self, url: str, retries: int = 2) -> str | None:
        import requests

        for attempt in range(retries + 1):
            try:
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 meridian"}, timeout=15)
                if r.status_code == 200:
                    return r.text
            except Exception:
                pass
            time.sleep(0.4 * (attempt + 1))  # backoff
        return None

    def _tag_symbols(self, title: str, ctx: IngestContext) -> tuple[str, ...]:
        symbols = set(ctx.universe_symbols)
        found: list[str] = []
        for tok in re.findall(r"\$?([A-Z]{1,5})\b", title):
            if tok in symbols and tok not in _STOP and tok not in found:
                found.append(tok)
        lower = title.lower()
        for row in ctx.universe:
            sym = row["symbol"]
            if sym in found:
                continue
            anchor = _name_anchor(row.get("name", ""))
            if anchor and anchor in lower:
                found.append(sym)
        return tuple(found)


def _entry_time(entry: Any) -> dt.datetime | None:
    import calendar

    # feedparser's *_parsed struct_time is UTC -> timegm (not mktime, which assumes local).
    for attr in ("published_parsed", "updated_parsed"):
        tm = entry.get(attr)
        if tm:
            return to_utc(dt.datetime.fromtimestamp(calendar.timegm(tm), tz=dt.timezone.utc))
    return None


def _name_anchor(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z ]", " ", name).strip()
    first = cleaned.split(" ")[0] if cleaned else ""
    return first.lower() if len(first) >= 4 else ""
