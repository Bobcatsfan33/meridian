"""Part C: Massive reliability harness (token bucket, circuit breaker, cache) +
grouped-daily parsing + precedence. The guarantee: Massive failures never escape."""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from tests.conftest import NOW

from meridian.adapters.base import IngestContext
from meridian.adapters.massive import (
    CircuitBreaker, MassiveAdapter, MassiveClient, ResponseCache, TokenBucket)

GROUPED = json.loads((pathlib.Path(__file__).parent / "fixtures" / "massive_grouped_20260626.json").read_text())
TARGET = dt.date(2026, 6, 26)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Transport:
    """Scripted transport: returns the next (status, body) and counts calls."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, url, params, timeout):
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _client(transport, clock, **kw):
    return MassiveClient(api_key="k", rate_limit_per_min=kw.pop("rate", 600),
                         transport=transport, clock=clock, sleep=clock.sleep,
                         breaker_threshold=kw.pop("threshold", 3), breaker_cooldown_s=kw.pop("cooldown", 300),
                         retries=kw.pop("retries", 1), cache_dir=kw.pop("cache_dir", None))


def test_token_bucket_throttles():
    clk = Clock()
    tb = TokenBucket(rate_per_min=2, clock=clk, sleep=clk.sleep)
    tb.acquire()
    tb.acquire()                     # 2 tokens consumed instantly
    t0 = clk()
    tb.acquire()                     # 3rd must wait for refill
    assert clk() > t0


def test_circuit_breaker_trips_and_recovers():
    clk = Clock()
    cb = CircuitBreaker(threshold=2, cooldown_s=100, clock=clk)
    assert cb.allow()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open" and not cb.allow()   # tripped, short-circuits
    clk.t += 101
    assert cb.allow() and cb.state == "half_open"  # cooldown elapsed
    cb.record_success()
    assert cb.state == "closed" and cb.allow()


def test_cache_roundtrip(tmp_path):
    c = ResponseCache(tmp_path)
    c.put("k", {"a": 1})
    assert c.get("k") == {"a": 1}
    assert c.get("missing") is None


def test_client_success_then_cache_served_on_outage(tmp_path):
    clk = Clock()
    # first call OK, then permanent 500s
    t = Transport([(200, {"v": 1}), (500, None), (500, None), (500, None), (500, None)])
    c = _client(t, clk, cache_dir=tmp_path, threshold=2, retries=0)
    assert c.get("/x") == {"v": 1}            # success cached
    # subsequent failures fall back to last-good cache, never raise
    assert c.get("/x") == {"v": 1}
    assert c.get("/x") == {"v": 1}


def test_429_trips_breaker_and_short_circuits():
    clk = Clock()
    t = Transport([(429, None)])               # always rate-limited
    c = _client(t, clk, threshold=2, retries=0, cache_dir=None)
    assert c.get("/a") is None                 # failure 1
    assert c.get("/b") is None                 # failure 2 -> breaker OPEN
    assert c.breaker.state == "open"
    calls_before = t.calls
    assert c.get("/c") is None                 # short-circuited (no transport call)
    assert t.calls == calls_before
    assert c.healthy is False


def test_timeout_never_raises():
    clk = Clock()
    t = Transport([TimeoutError("boom"), TimeoutError("boom")])
    c = _client(t, clk, threshold=5, retries=1, cache_dir=None)
    assert c.get("/x") is None                 # exception swallowed -> None


def _ctx():
    universe = ({"symbol": "AAPL", "name": "Apple", "sector": "Information Technology", "index_membership": "SP500"},)
    etfs = ({"symbol": "SPY", "role": "index", "description": "S&P 500"},)
    return IngestContext(trade_date=TARGET, now=NOW, universe=universe, etfs=etfs)


def test_adapter_no_key_is_silent_noop():
    a = MassiveAdapter(settings={})           # no client, no key
    assert a.fetch(_ctx()) == []


def test_adapter_grouped_daily_parse_and_data_source():
    clk = Clock()
    client = _client(Transport([(200, GROUPED)]), clk)
    a = MassiveAdapter(settings={}, client=client)
    raws, events = a.run(_ctx())
    tickers = {r.ticker for r in raws}
    assert tickers == {"AAPL", "SPY"}          # ZZZZ filtered (not in universe/etfs)
    aapl = next(e for e in events if e.ticker == "AAPL")
    assert aapl.event_type == "DailyBar" and aapl.payload["data_source"] == "massive"
    spy = next(e for e in events if e.ticker == "SPY")
    assert spy.family == "sector_peer"


def test_precedence_prefers_massive_over_yfinance():
    from meridian.ingest.pipeline import _resolve_precedence

    clk = Clock()
    a = MassiveAdapter(settings={}, client=_client(Transport([(200, GROUPED)]), clk))
    _raws, mas = a.run(_ctx())
    from meridian.adapters.yfinance import YFinanceAdapter
    yf = YFinanceAdapter()
    yf_aapl = yf.normalize(__import__("meridian.adapters.base", fromlist=["RawEvent"]).RawEvent(
        "yfinance", NOW, "AAPL", {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
                                  "role": "stock", "trade_date": TARGET.isoformat()}), _ctx())
    resolved = _resolve_precedence(mas + yf_aapl)
    aapl = [e for e in resolved if e.ticker == "AAPL"]
    assert len(aapl) == 1 and aapl[0].source == "massive"   # massive wins
