"""Ad-hoc single-name analysis (Step 4) for tickers NOT in the tracked universe.

Runs a SCOPED pipeline for exactly one symbol + date via the existing fail-safe adapters
and the unchanged engine: fetch a price window (the name + its sector ETF + the market) ->
build_state -> featurize -> match -> build the card. One symbol only (no fan-out on a typo);
network calls are fail-safe (return empty on failure); the result is cached so re-search is
instant. The card is labeled ad-hoc. No engine logic is changed.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

from .config import Config
from .ingest.clock import market_close_utc
from .storage import connect, db, init_db

_MARKET = "SPY"


def analyze(cfg: Config, ticker: str, target_date: dt.date, refresh: bool = False) -> dict[str, Any]:
    """Public entry: cached ad-hoc evidence object for ticker+date (network, fail-safe)."""
    ticker = (ticker or "").strip().upper()
    cache = _cache_path(cfg, ticker, target_date)
    if not refresh and cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            pass
    sector, sector_etf = _derive_sector(cfg, ticker)
    from .state.prices import fetch_yf_window

    symbols = [s for s in dict.fromkeys([ticker, sector_etf, _MARKET]) if s]
    start = target_date - dt.timedelta(days=int(cfg.feat("history_calendar_days", 200)))
    price_window = fetch_yf_window(symbols, start, target_date)   # fail-safe ({} on failure)
    news = _fetch_news(cfg, ticker, target_date)
    ev = build_adhoc(cfg, ticker, target_date, price_window, sector, sector_etf, news)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(ev, default=str))
    return ev


def build_adhoc(cfg: Config, ticker: str, target_date: dt.date, price_window: dict,
                sector: str | None, sector_etf: str | None, news: list | None = None) -> dict[str, Any]:
    """Deterministic core (offline given a price_window): scratch DB -> build_state ->
    featurize -> match -> card. Labeled ad-hoc. Testable without network."""
    from .engine.featurize import featurize
    from .engine.match import run_match
    from .outputs.build import build_explanations, card_for_ticker
    from .state.builder import build_state

    scratch = _scratch_db(cfg, ticker, target_date)
    init_db(scratch, cfg.universe_file)
    scfg = Config.load()
    scfg.raw.setdefault("storage", {})["duckdb_path"] = str(scratch)
    close = market_close_utc(target_date).replace(tzinfo=None)

    con = connect(scratch)
    try:
        # register the ad-hoc symbol so sector maps resolve (sector may be None)
        con.execute("DELETE FROM universe WHERE symbol=?", [ticker])
        con.execute("INSERT INTO universe (symbol, name, sector, index_membership) VALUES (?,?,?,?)",
                    [ticker, ticker, sector, "AD_HOC"])
        # symbol roles for build_state
        meta: dict[str, dict] = {ticker: {"kind": "stock", "role": "stock", "sector": sector}}
        if sector_etf:
            meta[sector_etf] = {"kind": "etf", "role": "sector", "sector_name": sector, "sector": None}
        meta[_MARKET] = {"kind": "etf", "role": "index", "sector_name": "S&P 500", "sector": None}

        if price_window.get(ticker):
            build_state(con, scfg, target_date, price_window, meta)   # ticker_state + regime + baseline

        # day-D normalized events: the move (+ sector ETF + news) so patterns can match
        _seed_day_events(con, ticker, sector, sector_etf, close, target_date, price_window, news or [])
    finally:
        con.close()

    with db(scratch) as fcon:           # fd-safe: featurize() does not own/close its con
        featurize(fcon, scfg, target_date)
    run_match(scfg, target_date)
    build_explanations(scfg, target_date)

    ev = card_for_ticker(scfg, ticker, target_date)
    ev["ad_hoc"] = True                       # label: ◆ Ad-hoc — not part of the tracked universe
    ev["data_source"] = "ad_hoc"
    if ev["pattern"]["id"] == "none":
        ev["pattern"]["description"] = "No supported explanation (ad-hoc)"
        ev["readout"] = "Ad-hoc read — moved in line with expectations; no supported pattern."
    return ev


def _seed_day_events(con, ticker, sector, sector_etf, close, target_date, price_window, news) -> None:
    def ins(eid, sym, etype, fam, ds, payload, et=None):
        con.execute(
            "INSERT OR REPLACE INTO normalized_events (event_id,event_time,ingest_time,ticker,"
            "event_type,family,source,confidence,sector,related_symbols,parent_event_id,"
            "data_source,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [eid, et or close, close, sym, etype, fam, "adhoc", 0.9,
             sector if sym == ticker else None, [], None, ds, json.dumps(payload)])

    if price_window.get(ticker):
        ins(f"ah_{ticker}", ticker, "DailyBar", "price_volume", "yfinance", {})
    if sector_etf and price_window.get(sector_etf):
        ins(f"ah_{sector_etf}", sector_etf, "ETFBar", "sector_peer", "yfinance", {})
    for i, n in enumerate(news or []):
        et = n.get("event_time") or close
        ins(f"ah_news_{i}", ticker, "HeadlineHit", "news", "news_rss",
            {"headline": n.get("headline", ""), "url": n.get("url", "")}, et=et)


# --- best-effort, fail-safe enrichers --------------------------------------------
def _derive_sector(cfg: Config, ticker: str) -> tuple[str | None, str | None]:
    """yfinance sector -> (sector, sector_etf). Best-effort; (None, None) on any failure."""
    sector = None
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info or {}
        sector = info.get("sector")
    except Exception:
        sector = None
    if not sector:
        return None, None
    etf = _sector_etf_for(cfg, sector)
    return sector, etf


def _sector_etf_for(cfg: Config, sector: str) -> str | None:
    """Map a (possibly yfinance-styled) sector name to a sector SPDR via index_etfs.csv."""
    import csv

    if not cfg.index_etf_file.exists():
        return None
    want = sector.strip().lower()
    with cfg.index_etf_file.open() as fh:
        for r in csv.DictReader(fh):
            if (r.get("role") or "") == "sector":
                desc = (r.get("description") or "").strip().lower()
                if desc == want or want in desc or desc in want:
                    return r["symbol"]
    return None


def _fetch_news(cfg: Config, ticker: str, target_date: dt.date) -> list[dict]:
    """Per-symbol Yahoo RSS for the date (fail-safe, single symbol)."""
    try:
        from .adapters.base import IngestContext
        from .adapters.news import NewsRssAdapter

        a = NewsRssAdapter({"scope": "movers", "watchlist": [ticker]})
        ctx = IngestContext(trade_date=target_date, now=dt.datetime.now(dt.timezone.utc),
                            universe=({"symbol": ticker, "name": ticker, "sector": None,
                                       "index_membership": "AD_HOC"},))
        out = []
        for raw, evs in [(r, a.normalize(r, ctx)) for r in a.fetch(ctx)]:
            for e in evs:
                out.append({"event_time": e.as_storage_row()["event_time"],
                            "headline": (e.payload or {}).get("headline", ""),
                            "url": (e.payload or {}).get("url", "")})
        return out
    except Exception:
        return []


def _cache_path(cfg: Config, ticker: str, target_date: dt.date) -> pathlib.Path:
    return cfg.root / "data" / "adhoc_cache" / f"{ticker}_{target_date.isoformat()}.json"


def _scratch_db(cfg: Config, ticker: str, target_date: dt.date) -> pathlib.Path:
    d = cfg.root / "data" / "adhoc"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{ticker}_{target_date.isoformat()}.duckdb"
