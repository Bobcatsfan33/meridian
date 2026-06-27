"""News RSS adapter (ROADMAP §6 news driver, coverage-limited free tier).

Parses configured RSS feeds (Reuters/PR/Benzinga-style), keeps entries published
on the trade date, and emits `HeadlineHit` events. A deterministic entity tagger
maps headlines to universe symbols by ticker token or company-name substring; this
is entity resolution, not a signal threshold, so it is allowed at ingestion.

Sentiment/topic classification is left to later phases (NLP) — we only record the
headline, its source, and the matched symbols here.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Adapter, IngestContext, RawEvent, NormalizedEvent
from ..ingest.clock import to_utc

# Reasonable free defaults; override via config adapters.news_rss.feeds.
DEFAULT_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.cnbc.com/id/10000664/device/rss/rss.html",
]
_STOP = {"A", "I", "AT", "BE", "ON", "OR", "IT", "IS", "AS", "SO", "TO", "UP", "USA", "CEO", "CFO", "IPO", "GDP", "FED", "ETF"}


class NewsRssAdapter(Adapter):
    name = "news_rss"
    source = "news_rss"
    default_family = "news"
    reliability = 0.80  # free RSS: coverage-limited, lower data-quality weight
    expected_latency_seconds = 15 * 60.0
    priority = 1

    def _feeds(self) -> list[str]:
        feeds = self.settings.get("feeds")
        return feeds if isinstance(feeds, list) and feeds else DEFAULT_FEEDS

    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        import feedparser

        out: list[RawEvent] = []
        for url in self._feeds():
            try:
                parsed = feedparser.parse(url)
            except Exception:
                continue
            for entry in parsed.entries:
                published = self._entry_time(entry)
                if published is None or published.date() != ctx.trade_date:
                    continue
                out.append(
                    RawEvent(
                        self.source,
                        ctx.now,
                        None,
                        {
                            "title": entry.get("title", "").strip(),
                            "link": entry.get("link", ""),
                            "feed": url,
                            "published_iso": published.isoformat(),
                        },
                    )
                )
        return out

    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        title = raw.payload["title"]
        if not title:
            return []
        matched = self._tag_symbols(title, ctx)
        primary = matched[0] if matched else None
        published = dt.datetime.fromisoformat(raw.payload["published_iso"])
        return [
            self._event(
                event_type="HeadlineHit",
                event_time=published,
                ingest_time=raw.ingest_time,
                ticker=primary,
                sector=ctx.sector_of(primary) if primary else None,
                related_symbols=matched,
                payload={
                    "headline": title,
                    "url": raw.payload["link"],
                    "feed": raw.payload["feed"],
                    "matched_symbols": list(matched),
                },
                id_extra=raw.payload["link"] or title,
            )
        ]

    # --- helpers ---------------------------------------------------------------
    @staticmethod
    def _entry_time(entry: Any) -> dt.datetime | None:
        import time

        for attr in ("published_parsed", "updated_parsed"):
            tm = entry.get(attr)
            if tm:
                return to_utc(dt.datetime.fromtimestamp(time.mktime(tm)))
        return None

    def _tag_symbols(self, title: str, ctx: IngestContext) -> tuple[str, ...]:
        """Deterministic entity match: explicit ticker tokens, then company names."""
        symbols = set(ctx.universe_symbols)
        found: list[str] = []
        # 1) bare uppercase tokens that are real symbols ($AAPL or AAPL)
        for tok in re.findall(r"\$?([A-Z]{1,5})\b", title):
            if tok in symbols and tok not in _STOP and tok not in found:
                found.append(tok)
        # 2) company-name substring (first significant word of the registered name)
        lower = title.lower()
        for row in ctx.universe:
            sym = row["symbol"]
            if sym in found:
                continue
            anchor = _name_anchor(row.get("name", ""))
            if anchor and anchor in lower:
                found.append(sym)
        return tuple(found)


def _name_anchor(name: str) -> str:
    """Significant leading token of a company name, lowercased (e.g. 'Apple')."""
    cleaned = re.sub(r"[^A-Za-z ]", " ", name).strip()
    first = cleaned.split(" ")[0] if cleaned else ""
    return first.lower() if len(first) >= 4 else ""
