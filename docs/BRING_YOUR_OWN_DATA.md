# Bring Your Own Data (BYOD)

How to feed Meridian **your** historical events. You implement (or copy) one small class —
an `Adapter` — that turns your raw records into Meridian's canonical typed events. Everything
downstream (featurize → match → explain → predict) then works unchanged.

There are **two hard rules** (stated plainly at the bottom). Get those right and your
evaluation is sound; get them wrong and the results are meaningless.

---

## The Adapter interface
From `meridian/adapters/base.py`. An adapter has two responsibilities, deliberately split so
the network/IO part stays out of the deterministic, testable part:

```python
class Adapter(ABC):
    name: str                       # CLI id, e.g. "my_csv"
    source: str                     # provenance label written on every row, e.g. "my_csv"
    default_family: str = "price_volume"
    reliability: float = 0.9        # data-quality confidence in [0,1] (NOT a signal threshold)
    expected_latency_seconds: float = 60.0   # typical event_time -> ingest_time delay
    priority: int = 1               # 1=free, 2=secondary, 3=paid

    @abstractmethod
    def fetch(self, ctx: IngestContext) -> list[RawEvent]:
        """Pull raw payloads for ctx.trade_date (your IO). Never raise on a partial
        failure — return what you got so coverage can be reported."""

    @abstractmethod
    def normalize(self, raw: RawEvent, ctx: IngestContext) -> list[NormalizedEvent]:
        """PURE transform of one raw payload into >=0 canonical events. No IO, no clock —
        deterministic given its inputs, so it can be golden-tested."""

    def run(self, ctx) -> tuple[list[RawEvent], list[NormalizedEvent]]:
        """Default: fetch() then normalize() each. You rarely override this."""

    # convenience for normalize(): builds a NormalizedEvent with a stable id + UTC times
    def _event(self, *, event_type, event_time, ingest_time, ticker, payload,
               family=None, sector=None, related_symbols=(), id_extra="") -> NormalizedEvent: ...
```

**`IngestContext`** (read-only inputs handed to your adapter for one run):

| field | meaning |
|---|---|
| `trade_date` | the date being ingested (`datetime.date`) |
| `now` | injected wall clock (tz-aware UTC) — use it as `ingest_time` so tests are deterministic |
| `universe` | tuple of `{symbol, name, sector, index_membership}` dicts (S&P 500 ∪ Nasdaq-100) |
| `etfs` | tuple of `{symbol, role, description}` dicts (index/sector/macro ETFs) |
| `settings` | your adapter's config block from `config/config.yaml` (e.g. a file path) |

Helpers: `ctx.universe_symbols` (tuple of symbols), `ctx.sector_of(symbol)`.

**`RawEvent`** — one payload exactly as pulled, plus arrival time:
`source`, `ingest_time` (tz-aware UTC), `ticker`, `payload` (dict). `.raw_id` is a stable
content hash (excludes ingest_time) so re-runs upsert instead of duplicating.

---

## The canonical event schema (`NormalizedEvent`, field by field)
Every adapter must emit this shape. (Stored in DuckDB table `normalized_events`.)

| field | type | required | meaning |
|---|---|---|---|
| `event_id` | str | yes | **stable** id; identical content → identical id (dedup/idempotency). Use `make_event_id(...)` or `_event(...)`. |
| `event_time` | datetime (tz-aware UTC) | yes | **when it happened**, on the source clock after alignment. The no-lookahead anchor. |
| `ingest_time` | datetime (tz-aware UTC) | yes | **when you received it** (= `ctx.now`). Must be ≥ `event_time` (minus small skew). |
| `ticker` | str \| None | for per-name events | the symbol the event pertains to. |
| `event_type` | str | yes | e.g. `DailyBar`, `HeadlineHit`, `Filing8K`, `ShortVolumeSpike` (free-form, but be consistent). |
| `family` | str | yes | one of the canonical families (below). Validated — an unknown family raises. |
| `source` | str | yes | provenance label (your `source`). |
| `confidence` | float [0,1] | yes | data-quality / source reliability — NOT a signal threshold. |
| `sector` | str \| None | optional | GICS sector (use `ctx.sector_of(ticker)`). |
| `related_symbols` | tuple[str] | optional | other symbols mentioned/affected. |
| `parent_event_id` | str \| None | optional | reserved (membership in a complex event). Leave `None`. |
| `payload` | dict | yes | the typed details (e.g. `{open, high, low, close, volume}` or `{headline, url}`). JSON-serializable. |

`data_source` (provenance vocabulary) is derived for you from `source`/`payload` and written
on every row — you don't set it directly.

### Canonical families (the only valid `family` values)
`price_volume`, `sector_peer`, `macro`, `options_flow`, `dealer_pos`, `news`, `filing`,
`earnings`, `liquidity`, `attention`, `equity_flow`.

> **All thresholds live in Layer-1 featurization, never in your adapter.** Emit the objective
> observation (a bar, a headline, a short-volume number). Meridian grades abnormality against
> the name's own regime baseline. Do not pre-filter to "big" events or bake in cutoffs.

---

## The two hard rules (get these right)

### 1. Correct `event_time` — no lookahead
`event_time` is **when the event actually happened**, not when you loaded it. A daily bar's
`event_time` is the session close; a headline's is its publish time; a filing's is its
acceptance time. The engine uses event-time (after clock alignment), never arrival order, and
features at time *t* must never use data with `event_time > t`. If you stamp `event_time` with
"now" (a common parser fallback), you destroy the temporal signal and silently leak the future.
Always set `ingest_time = ctx.now` and a *real* `event_time` from your data — in **UTC**.

### 2. Point-in-time correctness — no survivorship
Provide data **as it existed on each historical date**, including names later delisted/renamed
and values *as first reported* (not later restated). A universe of "today's survivors" or
back-adjusted/restated figures makes every backtest look better than reality. If your history
can't be made point-in-time, say so in your feedback — it bounds what the evaluation can claim.

---

## Minimal worked example
See [`examples/csv_adapter.py`](examples/csv_adapter.py) (a real, runnable adapter over the CSV
schema `event_time,ticker,event_type,family,value,payload_json`) and
[`examples/sample_events.csv`](examples/sample_events.csv). Copy it, change the parsing to your
format, keep `normalize()` pure, and you're done. The golden test
`tests/test_example_csv_adapter.py` shows the invariants you should preserve.
