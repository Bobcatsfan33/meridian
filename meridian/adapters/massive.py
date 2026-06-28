"""Massive (formerly Polygon.io) OPTIONAL fail-safe adapter (Part C).

Meridian must NEVER break without Massive. This module is enabled-opt-in (config
adapters.massive.enabled + MASSIVE_API_KEY) and wraps every call in a reliability harness:
  * token-bucket throttle (rate_limit_per_min, free tier = 5/min),
  * circuit breaker (trip OPEN after N consecutive failures/429s; half-open after cooldown),
  * bounded retries with backoff + timeouts,
  * local last-good response cache (reused on a brief outage / re-run),
  * EVERY call returns empty on failure — no exception escapes into run-day.

Massive serves the Polygon-compatible REST API (api.massive.com == api.polygon.io).
Endpoints are config-overridable; defaults target the documented free Basic plan
(EOD/grouped-daily aggregates, options chain snapshot).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import Adapter, IngestContext, NormalizedEvent, RawEvent
from ..ingest.clock import market_close_utc

DEFAULT_BASE_URL = "https://api.massive.com"
GROUPED_DAILY = "/v2/aggs/grouped/locale/us/market/stocks/{date}"
OPTION_CHAIN = "/v3/snapshot/options/{underlying}"


# --- reliability primitives (pure, clock-injectable; unit-tested) ----------------
class TokenBucket:
    def __init__(self, rate_per_min: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.capacity = max(1.0, float(rate_per_min))
        self.rate = self.capacity / 60.0
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.last = clock()
        self.total_wait = 0.0

    def acquire(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1.0:
            wait = (1.0 - self.tokens) / self.rate
            self.total_wait += wait
            self.sleep(wait)
            self.tokens = 0.0
            self.last = self.clock()
        else:
            self.tokens -= 1.0


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown_s: float, clock: Callable[[], float],
                 on_trip: Callable[[], None] | None = None):
        self.threshold = max(1, int(threshold))
        self.cooldown = float(cooldown_s)
        self.clock = clock
        self.on_trip = on_trip
        self.failures = 0
        self.state = "closed"   # closed | open | half_open
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.state == "open":
            if self.opened_at is not None and self.clock() - self.opened_at >= self.cooldown:
                self.state = "half_open"
                return True
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.threshold:
            if self.state != "open" and self.on_trip:
                self.on_trip()
            self.state = "open"
            self.opened_at = self.clock()


class ResponseCache:
    def __init__(self, cache_dir: pathlib.Path | None):
        self.dir = pathlib.Path(cache_dir) if cache_dir else None

    def _path(self, key: str) -> pathlib.Path | None:
        if self.dir is None:
            return None
        h = hashlib.sha1(key.encode()).hexdigest()[:24]
        return self.dir / f"{h}.json"

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        if p and p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None

    def put(self, key: str, value: Any) -> None:
        p = self._path(key)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(value, default=str))
        except Exception:
            pass


# transport: (url, params, timeout) -> (status_code, json_body|None). Injectable for tests.
Transport = Callable[[str, dict, float], "tuple[int, Any]"]


def _requests_transport(url: str, params: dict, timeout: float) -> tuple[int, Any]:
    import requests

    r = requests.get(url, params=params, timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = None
    return r.status_code, body


@dataclass
class MassiveClient:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    rate_limit_per_min: float = 5.0
    timeout: float = 15.0
    retries: int = 2
    cache_dir: pathlib.Path | None = None
    transport: Transport = _requests_transport
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 300.0
    _tripped_logged: bool = field(default=False, init=False)

    def __post_init__(self):
        self.bucket = TokenBucket(self.rate_limit_per_min, self.clock, self.sleep)
        self.breaker = CircuitBreaker(self.breaker_threshold, self.breaker_cooldown_s,
                                      self.clock, on_trip=self._on_trip)
        self.cache = ResponseCache(self.cache_dir)

    def _on_trip(self) -> None:
        if not self._tripped_logged:  # log once, don't spam
            import logging
            logging.getLogger("meridian.massive").warning(
                "Massive circuit breaker OPEN after %d failures — falling back.", self.breaker.failures)
            self._tripped_logged = True

    def get(self, endpoint: str, params: dict | None = None, cache_key: str | None = None) -> Any | None:
        """Fetch JSON. NEVER raises. Returns last-good cache on failure/open breaker, else None."""
        params = dict(params or {})
        key = cache_key or (endpoint + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())))
        if not self.breaker.allow():
            return self.cache.get(key)  # breaker open -> serve last-good if we have it
        self.bucket.acquire()
        url = self.base_url + endpoint
        call_params = {**params, "apiKey": self.api_key}
        for attempt in range(self.retries + 1):
            try:
                status, body = self.transport(url, call_params, self.timeout)
                if status == 200 and body is not None:
                    self.breaker.record_success()
                    self.cache.put(key, body)
                    return body
            except Exception:
                pass
            if attempt < self.retries:
                self.sleep(0.4 * (attempt + 1))  # backoff
        self.breaker.record_failure()
        return self.cache.get(key)  # last-good fallback (may be None)

    @property
    def healthy(self) -> bool:
        return self.breaker.state != "open"


def client_from_config(cfg, cache_dir: pathlib.Path | None = None) -> MassiveClient | None:
    """Build a MassiveClient from config + MASSIVE_API_KEY, or None if not enabled/keyed."""
    block = (cfg.raw.get("adapters", {}) or {}).get("massive", {}) or {}
    if not block.get("enabled"):
        return None
    key = os.environ.get(block.get("api_key_env", "MASSIVE_API_KEY"))
    if not key:
        return None
    cb = block.get("circuit_breaker", {}) or {}
    return MassiveClient(
        api_key=key,
        base_url=block.get("base_url", DEFAULT_BASE_URL),
        rate_limit_per_min=float(block.get("rate_limit_per_min", 5)),
        timeout=float(block.get("timeout", 15)),
        retries=int(block.get("retries", 2)),
        cache_dir=cache_dir or (cfg.root / "data" / "massive_cache"),
        breaker_threshold=int(cb.get("threshold", 3)),
        breaker_cooldown_s=float(cb.get("cooldown_seconds", 300)),
    )


# --- stock daily bars via grouped-daily (Step C2) --------------------------------
class MassiveAdapter(Adapter):
    name = "massive"
    source = "massive"
    default_family = "price_volume"
    reliability = 0.97
    expected_latency_seconds = 0.0
    priority = 2

    def __init__(self, settings: dict | None = None, client: MassiveClient | None = None):
        super().__init__(settings)
        self._client = client

    def _get_client(self, ctx: IngestContext) -> MassiveClient | None:
        if self._client is not None:
            return self._client
        from ..config import Config

        # settings is the adapter block; reconstruct a minimal cfg view for the factory
        cfg = Config(raw={"adapters": {"massive": {**(self.settings or {}), "enabled": True}}})
        return client_from_config(cfg)

    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        self.fetch_failures = 0
        client = self._get_client(ctx)
        if client is None:
            return []  # not enabled/keyed -> silent no-op, pipeline falls back
        targets: dict[str, str] = {s: "stock" for s in ctx.universe_symbols}
        for row in ctx.etfs:
            if (row.get("role") or "") != "macro":  # macro indices aren't in the stocks market
                targets[row["symbol"]] = row.get("role", "sector")
        body = client.get(GROUPED_DAILY.format(date=ctx.trade_date.isoformat()), {"adjusted": "true"})
        if not body or not isinstance(body, dict):
            self.fetch_failures = 1
            return []
        out: list[RawEvent] = []
        for rec in body.get("results", []) or []:
            sym = rec.get("T")
            if sym not in targets:
                continue
            out.append(RawEvent(self.source, ctx.now, sym, {
                "open": rec.get("o"), "high": rec.get("h"), "low": rec.get("l"),
                "close": rec.get("c"), "volume": rec.get("v"),
                "role": targets[sym], "trade_date": ctx.trade_date.isoformat()}))
        return out

    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        from .yfinance import _role_to_event_type, _role_to_family

        role = raw.payload.get("role", "stock")
        trade_date = dt.date.fromisoformat(raw.payload["trade_date"])
        if raw.payload.get("close") is None:
            return []
        if role == "stock":
            family, event_type, sector = "price_volume", "DailyBar", ctx.sector_of(raw.ticker)
        else:
            family, event_type, sector = _role_to_family(role), _role_to_event_type(role), None
        payload = {k: raw.payload[k] for k in ("open", "high", "low", "close", "volume")
                   if raw.payload.get(k) is not None}
        payload["role"] = role
        payload["data_source"] = "massive"
        return [self._event(event_type=event_type, event_time=market_close_utc(trade_date),
                            ingest_time=raw.ingest_time, ticker=raw.ticker, family=family,
                            sector=sector, payload=payload)]
