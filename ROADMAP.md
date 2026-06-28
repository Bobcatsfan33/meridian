# Market Event Correlation & Prediction Engine — Local Build Roadmap

**A Claude Code build specification**
Working repo name: `meridian` (rename freely)
Prepared for: HCA Strategy LLC · June 27, 2026

---

## 0. How to use this document

This is an **executable build spec written to be handed to Claude Code.** It is ordered so a coding agent can build it phase by phase, with each phase producing something you can run locally that same day. Drop this file into an empty repo as `ROADMAP.md`, create the `CLAUDE.md` in Section 16, and drive the build phase by phase ("Claude, build Phase 0," then "Phase 1," etc.).

Every phase has: a **goal**, the **modules to create**, **acceptance criteria** (how Claude Code knows it's done), and a **run command** (what you type to see it work).

**North star.** A locally-run engine that ingests every meaningful market event driver, correlates them in a partial-order (poset) causal graph, separates the *initiating* event from confirmation/contradiction/noise, attributes the move with an auditable evidence trail, and — the predictive layer — scores how that pattern has historically resolved into forward returns, always reporting what it *cannot* explain.

**What this is not.** Not a guaranteed predictor, not proof of causality, not automated execution. It compresses analysis and ranks the best-supported explanation with calibrated forward odds. Human in the loop decides.

---

## 1. Operating principles (non-negotiable, enforced in code)

These are constraints the build must structurally enforce, not aspirations.

1. **Evidence first.** Every claim on a card maps back to source events with timestamps and lineage (a `rule_id` and `event_edges` row).
2. **Probabilistic language only.** "Most supported explanation," "likely," "appears." Never "proves" / "the cause was."
3. **Partial order, not arrival order.** Every event carries `event_time` AND `ingest_time`. Correlation logic uses event-time after clock alignment; arrival order is never trusted.
4. **Graded, not boolean.** Patterns return a completeness score in [0,1], never true/false. (This is the deliberate fork from a purely deterministic engine — markets are probabilistic.)
5. **Admit the unexplained.** Every output reports the share of the abnormal move it could not attribute. The explanation is never rounded to 100%.
6. **Falsifiability.** Every read ships with an explicit invalidation line — what would have to happen for the read to be wrong.
7. **Causality is inferred, never asserted.** Edges in the graph are *statistically tested* (lead-lag / Granger / transfer-entropy gated) before being trusted, and still reported as probable, not proven.
8. **The explanation layer can only print what's in the structured evidence object.** No free-form narrative invention.

---

## 2. Competitive wedge (what we build that DolphinSight / Ghospider does not)

A competitor (Beautiful Majestic Dolphin's DolphinSight, on their "goRapide" engine) already ships causal-graph + hypothesis hit-rate + paper backtest for equities. We do not chase their deterministic causal-language. We win on three axes they leave open:

| Axis | Them | Us (this build) |
|---|---|---|
| Matching | Purely deterministic causal links | **Graded, regime-aware probabilistic partial matching** (better fit for noisy/reflexive markets) |
| Mechanical flow | Price/macro/earnings/news | **Dealer-positioning / gamma layer + mechanical-vs-informational split** (the execution-edge piece) |
| Surface | Research/hypothesis console (pull) | **Real-time "why is it moving" cards with residual + invalidation (push), plus EOD postmortem** |

Everything below is designed around owning those three.

---

## 3. System architecture (local, daily, batch-now / intraday-ready)

```
                 ┌─────────────── Data Adapters (Section 6) ──────────────┐
                 │ yfinance · SEC EDGAR · free news · Robinhood MCP ·      │
                 │ macro (FRED) · options chains · (optional paid feeds)   │
                 └───────────────────────────┬────────────────────────────┘
                                             │  raw payloads
                                             ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ INGESTION & NORMALIZATION  → typed market events (dual timestamps)     │
   └───────────────────────────┬──────────────────────────────────────────┘
                               ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ STATE BUILDER  → rolling 1m/5m/EOD ticker, sector, options, liquidity  │
   │                  state + expected-behavior (beta/macro) baseline       │
   └───────────────────────────┬──────────────────────────────────────────┘
                               ▼
   ┌──────────── THREE-LAYER CORRELATION ENGINE (Section 9) ───────────────┐
   │  L1 Featurization   raw → graded events (abnormality vs own regime)    │
   │  L2 Structural      poset-native partial match (precedes/concurrent/   │
   │                     independent/contradicts) → completeness score      │
   │  L3 Scoring         calibrated confidence + driver attribution         │
   └───────────────────────────┬──────────────────────────────────────────┘
                               ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ EVIDENCE GRAPH  nodes, tested edges, confidence, rule_id, lineage      │
   └───────────────────────────┬──────────────────────────────────────────┘
                               ▼
   ┌──────────── PREDICTIVE ENGINE (Section 12) ──────────────────────────┐
   │ forward-return labeling · regime conditioning · walk-forward          │
   │ calibration · causal hit-rate · honest paper backtest                 │
   └───────────────────────────┬──────────────────────────────────────────┘
                               ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ EXPLANATION + OUTPUTS  deterministic structured cards (no LLM),        │
   │                        scanner, EOD postmortem, residual, invalidation │
   └──────────────────────────────────────────────────────────────────────┘

   Storage throughout: DuckDB (embedded columnar). Orchestration: local scheduler.
```

**Why this stack for a single local user:** no Kafka, no ClickHouse cluster, no microservices. The competitor's "engine" is a window-function + scoring problem at the latency we need (explanation in seconds-to-minutes, not microseconds). DuckDB gives ClickHouse-class window functions embedded in-process.

---

## 4. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | **Python 3.11+** | one language end-to-end |
| Store | **DuckDB** (file-backed) | columnar, real window functions, zero infra; one `.duckdb` file |
| In-memory compute | **Polars** | fast featurization/joins |
| Scheduling | **APScheduler** (in-proc) + optional OS cron/launchd | pre-market, intraday loop, post-close |
| Data adapters | `yfinance`, `sec-edgar` (or `sec-api` free tier), `feedparser` (RSS news), `fredapi` (macro), Robinhood MCP, pluggable paid adapters | all behind a common `Adapter` interface |
| Options analytics | `mibian` / custom Black-Scholes greeks; GEX proxy from chain OI+gamma | computed locally from chains |
| ML / calibration | `scikit-learn` (logistic, gradient-boosted trees), `statsmodels` (Granger), `scipy` | explainable models only |
| Explanation | **deterministic templates only** (Jinja, no LLM) | see Section 15 — fully deterministic, cannot hallucinate |
| UI | **FastAPI + single-page HTML dashboard**; CLI via `typer` | browser-openable, local only |
| Config | `pydantic-settings` + `.env` + `config.yaml` | universe, feeds, thresholds, run modes |
| Tests | `pytest` + golden-file fixtures | deterministic engine = testable |

---

## 5. Universe & run modes (from your answers)

- **Universe:** `S&P 500 ∪ Nasdaq-100` (~520 unique names). Maintained in `config/universe.csv`, refreshed weekly from public constituents. Sector/peer maps and index ETFs (SPY, QQQ, sector SPDRs, SMH, etc.) derived from it.
- **Run modes (build both):**
  - **EOD batch (primary):** pre-market scan (~8:30 ET) + post-close postmortem (~16:30 ET). No streaming infra. This is the daily driver.
  - **Intraday near-real-time (switch-on):** polling loop (default 1–5 min) producing live "why is it moving" cards. Same engine, fed by a scheduler instead of a batch job. Architecture is identical; only the trigger differs.

---

## 6. Data sources & driver coverage (free-first, Robinhood, paid only if essential)

Common `Adapter` interface so feeds are swappable. Priority order per driver = **free → Robinhood MCP → paid (only where the final-state product genuinely requires it)**.

| Event driver family | Free source | Robinhood MCP | Paid (last resort) | Buildable now? |
|---|---|---|---|---|
| Price / volume / candles | yfinance, Stooq | `get_equity_quotes`, `get_equity_historicals` | Polygon/Databento | ✅ Yes |
| Sector / peers / ETFs | yfinance (ETF prices) | quotes | — | ✅ Yes |
| Index / macro | yfinance (^VIX, ^TNX, DXY), **FRED** (yields, CPI, fed) | `get_index_quotes` | — | ✅ Yes |
| Earnings | yfinance calendar, EDGAR | `get_earnings_calendar`, `get_earnings_results` | — | ✅ Yes |
| Filings (8-K, S-3, 13D/G, Form 4) | **SEC EDGAR (free, full)** | — | — | ✅ Yes |
| News / headlines | RSS (Reuters/Benzinga/PR feeds), Finnhub free tier, GDELT | — | Benzinga Pro, paid newswire | ✅ Yes (coverage-limited) |
| Options flow | — (no good free real-time) | `get_option_chains`, `get_option_quotes`, `get_option_instruments` | ThetaData (history), Unusual Whales | ⚠️ Partial via Robinhood snapshots |
| **Dealer positioning / GEX** | **Computed locally** from option-chain OI + greeks | chain data feeds the proxy | SpotGamma/Menthor Q (licensed, only for institutional-grade accuracy) | ✅ Proxy buildable; paid only for fidelity |
| Liquidity (spread/depth/halts) | yfinance spread proxy, NASDAQ halt feed (free) | quotes (bid/ask) | full order book (paid) | ⚠️ Proxy now, book later |
| Attention (social/search) | Google Trends (`pytrends`), Reddit/StockTwits free | — | — | ✅ Yes (weighted low, never primary) |

**The one honest paid flag:** institutional-grade dealer GEX (SpotGamma/Menthor Q) is licensed. **You do not need it to ship** — we compute a GEX proxy from option-chain open interest and per-strike gamma (available free / via Robinhood). Recommend a paid options-history feed (ThetaData, ~$50/mo) *only* in Phase 6 if backtest fidelity on the gamma patterns proves insufficient. Everything else is free-first.

---

## 7. Event ontology / data model

Canonical typed event (every adapter must emit this shape):

```json
{
  "event_id": "evt_...",
  "event_time": "2026-06-22T10:14:03.120Z",   // when it happened (source clock, aligned)
  "ingest_time": "2026-06-22T10:14:03.410Z",   // when we received it
  "ticker": "AMD",
  "event_type": "options_sweep",
  "source": "robinhood_options",
  "confidence": 0.92,        // data-quality / source reliability
  "abnormality": 0.97,       // graded vs THIS name's own regime baseline (filled by L1)
  "sector": "semiconductors",
  "related_symbols": ["NVDA", "AVGO", "SMH"],
  "parent_event_id": null,   // membership in a higher-level complex event (reserved)
  "regime_tags": ["high_vol", "uptrend"],   // filled by state builder
  "payload": { "side": "call", "premium": 850000, "expiration": "2026-07-17", "strike": 180 }
}
```

`parent_event_id` is reserved from day one so intraday events can later roll into multi-day complex events (e.g., "third leg of a sector rotation") without a schema refactor.

### Event families to implement (encompass ALL drivers)

| Family | Event types | Purpose |
|---|---|---|
| Price / volume | `PriceMove`, `VWAPReclaim`, `HighOfDayBreak`, `RelVolumeSpike`, `GapUp/Down` | identify the move + technical confirmation |
| Options flow | `CallSweep`, `PutSweep`, `IVExpansion`, `OICluster`, `SkewShift` | predictive vs reactive flow |
| Dealer positioning | `ShortGamma`, `GammaFlip`, `CallWall`, `PutWall`, `VannaCharmFlow`, `OPEXPin` | mechanical driver (the differentiator) |
| News | `HeadlineHit`, `TopicTag`, `SentimentShift`, `SourcePriority` | did news lead or trail |
| Filings | `Filing8K`, `FilingS3`, `Filing13D`, `Filing13F`, `Form4Insider`, `ShelfReg` | structural company catalysts |
| Sector / peers | `SectorMove`, `PeerMove`, `ETFVWAPBreak` | company-specific vs basket/sympathy |
| Macro | `YieldMove`, `FedSpeaker`, `CPIPrint`, `DollarMove`, `VIXTermShift` | broad risk-on/off |
| Liquidity | `SpreadWidening`, `BookImbalance`, `Halt`, `ThinDepth` | thin-liquidity moves (snapback risk) |
| Attention | `SocialSpike`, `SearchSpike`, `NewsVelocity` | attention (weighted low, never primary) |

---

## 8. Storage design (DuckDB tables)

```
raw_market_events        normalized_events        graded_events
event_edges (AUDIT)      pattern_firings          move_explanations
ticker_state_1m          sector_state_1m          options_state_1m
expected_behavior_1m     news_events              filing_events
regimes_daily            historical_pattern_outcomes   paper_trades
calibration_curves       gex_surface
```

Most important audit table:

```sql
event_edges(
  src_event_id, dst_event_id, ticker,
  edge_type,        -- precedes | concurrent | independent | contradicts
  lag_seconds,
  test_stat,        -- lead-lag / Granger / transfer-entropy score
  test_pvalue,      -- edge only "trusted" if it passes the gate
  confidence,
  rule_id,          -- which named, versioned pattern asserted this edge
  created_at
)
```

Rolling state is maintained by **incremental refresh on insert** (DuckDB views / scheduled `INSERT ... SELECT`), so pattern matching is a window-function query, not a per-query recompute.

---

## 9. The three-layer correlation engine

### Layer 1 — Featurization (where ALL thresholds live)
Raw events → graded typed events. Each event gets a continuous `abnormality` in [0,1] computed against **the name's own regime baseline** (percentile or z-score over a trailing window), never a global hard cutoff. A 97th-percentile sweep for this ticker in this vol regime is signal; the same notional in a quiet large-cap is noise. No pattern hard-codes a dollar threshold.

### Layer 2 — Structural matching (poset-native, partial score)
Declarative, named, versioned patterns over the event graph. Operators:
- `precedes(A,B)` — A verified before B after clock alignment.
- `concurrent(A,B)` — within a window, order not asserted.
- `independent(A,B)` — explicitly no dependency.
- `contradicts(A,B)` — B runs against what A implies (market rejecting the narrative).

A match returns `completeness = matched_legs / total_legs`, weighted by abnormality — **never a boolean**. Implemented as parameterized SQL window-function templates + a small matcher in Python.

### Layer 3 — Scoring (calibrated, explainable)
Combines completeness, graded features, lead-lag strength, corroboration count, and historical hit-rate into:
- `confidence` (calibrated probability via logistic / GBM)
- `driver_attribution` = normalized per-feature contributions (auditable; every weight expands to its four inputs: lead-lag strength, corroboration count, signal abnormality, historical hit-rate in regime).

**ML belongs offline.** Unsupervised mining proposes candidate patterns from history; a human canonicalizes good ones into the named, versioned library. ML finds, humans codify, runtime stays explainable.

---

## 10. Pattern library (initial, versioned)

| Pattern | Skeleton (L2) | Trader readout | Main limitation |
|---|---|---|---|
| News-led | `NewsHit precedes PriceMove + RelVolumeSpike` | catalyst-driven | news latency / coverage |
| Price-before-news | `PriceMove precedes first NewsHit (min lag)` | headline is late confirmation | misses private/paid news |
| Options-led momentum | `CallSweep/IVExpansion precedes price expansion` | flow may have ignited move | options can be hedges |
| **Dealer gamma squeeze** | `ShortGamma + SpotIntoStrike, sweeps precede expansion, IV confirms, contradicts MacroShock` | mechanical, dealer-hedging driven | needs positioning data; decays as gamma normalizes |
| Sector sympathy | `Sector/peer move precedes target, no company news` | basket/sympathy | sector classification imperfect |
| Contradictory reaction | `Price contradicts headline sentiment` | market rejecting narrative | needs sentiment parsing |
| Liquidity vacuum | `Spread/depth deterioration concurrent with rapid move` | high snapback risk | needs depth data |

Worked example (gamma squeeze, three layers):
```
// L1 featurization
ShortGamma(T)      where dealer_gamma_pctile < 0.10
CallSweep(T)       graded abnormality vs T's own regime baseline
IVExpansion(T)     graded vs trailing IV distribution
SpotIntoStrike(T)  spot within k*ATR of a gamma strike cluster

// L2 structural match (partial score)
pattern GammaSqueeze(T) =
    ShortGamma(T) precedes SpotIntoStrike(T)
    precedes CallSweep(T) [repeat >= 3, abnormality high]
    concurrent IVExpansion(T)
    contradicts MacroShock(window)
  => completeness = weighted matched_legs / total_legs

// L3 scoring
confidence = calibrated_model(completeness, lead_lag_strength,
             corroboration_count, feature_abnormality,
             historical_hit_rate(GammaSqueeze, regime))
driver_attribution = normalized per-feature contributions
```

---

## 11. Honesty layer (constraints + residual + invalidation)

**Expected-behavior baseline & residual.** Before attributing anything, compute how the name *should* have moved given beta + the day's macro. The **abnormal portion** (move beyond expectation) is the denominator. Whatever matched patterns can't account for is reported as an explicit **unexplained residual on every output.**

**Constraint rules (block/downgrade bad explanations):**
- Not news-led if price moved materially before the first detected headline.
- Options flow not predictive if most activity occurred after the move.
- Not sector sympathy if the target moved before the sector ETF and peers.
- Not company-specific if the full peer group moved first with no company catalyst.
- No high confidence when major feeds are delayed/missing/conflicting.
- Explanation layer may assert nothing absent from the structured evidence object.

**Confidence tiers:** High / Medium / Low / Unknown, each with prescribed trader handling and language softening.

---

## 12. Predictive engine (the core of "prediction," done honestly)

This is where event correlation becomes forward odds — and where the competitor's "causal hit-rate" gets beaten by being graded, regime-aware, and residual-reporting.

**12.1 Labeling.** For every historical pattern firing, label forward outcomes at multiple horizons: `+5m, +30m, +EOD, +1d, +3d`. Record **MFE/MAE** (max favorable/adverse excursion) per firing.

**12.2 Regime conditioning.** Tag each firing with regime: VIX level + term structure, index trend, sector trend, breadth. Outcomes are always conditioned on regime — a pattern that paid in low-vol can invert in stress.

**12.3 Forward-return model.** Per pattern × regime, produce:
- `P(forward_return > threshold | pattern, regime, completeness)`
- expected return **distribution** (not point estimate), hit-rate, and **decay profile** (how long the edge persists).

**12.4 Causal-link testing (answers "correlation vs causation").** Graph edges are not trusted on co-occurrence. Each candidate `precedes` edge is gated by a statistical lead-lag test (Granger causality / transfer entropy / conditional lead-lag with controls). Only edges passing the gate (`test_pvalue` < α) become trusted causal edges; others are downgraded to `concurrent`/`independent`. This is the rigorous version of DolphinSight's "causal hit-rate," plus honesty.

**12.5 Walk-forward validation.** Purged, walk-forward (no lookahead): train calibration on past windows, validate on future windows, slide forward. Report reliability curves (predicted vs realized probability).

**12.6 Honest paper backtest.** For a fired pattern, open a paper position off the signal, resolve against the real price series, report win-rate and mean return **with the unexplained residual attached** — so the backtest is the honest version, never a clean story.

**Output contract of the predictive engine:** `(forward_distribution, hit_rate, decay, regime, confidence, residual, invalidation)`. Prediction is always presented as conditional odds + what would invalidate it, never a guarantee.

---

## 13. Trader-facing outputs

**A. "Why is it moving?" card** (deterministic template render, no LLM):
```
CEG  +7.1%   11:40 - 13:05 ET
Most supported explanation: Dealer-driven gamma squeeze (mechanical)   Confidence: 71%
Forward odds (this pattern, high-vol regime): +EOD median +0.6%, hit-rate 58%, decays ~90m

Evidence (partial order, not arrival order):
  11:38  Spot grinds into 290 strike cluster (dealers short gamma)
  11:41  Call sweep cluster begins (4x, >97th pctile)   -> hedging buys
  11:44  IV on front strikes lifts (confirms hedging, not info)
  11:58  First headline crosses   -> classified LATE confirmation
  12:10  Momentum follow-through into thin midday book

Unexplained residual: ~18% of the move is unattributed
Invalidation: IV bleeds while spot holds, or loses VWAP as flow fades, or sector reverses
```

**B. Pattern scanner** — ranked table across the universe: ticker, move, pattern, confidence, initiating event, forward odds, next watch.

**C. Daily market postmortem** — top price-before-news and dealer-driven mechanical moves; sector contagion chains; moves where price contradicted headline sentiment; which patterns worked/failed and the residual each left.

All three are produced by both run modes (intraday for A/B, post-close for C).

---

## 14. Repo structure (what Claude Code scaffolds in Phase 0)

```
meridian/
  CLAUDE.md                 # agent instructions (Section 16)
  ROADMAP.md                # this file
  config/
    config.yaml             # universe, feeds, thresholds, run modes
    universe.csv
    patterns/               # versioned pattern definitions (yaml)
  meridian/
    adapters/               # one module per feed; common Adapter interface
      base.py  yfinance.py  edgar.py  news.py  fred.py  robinhood_mcp.py
    ingest/                 # normalization → typed events
    state/                  # rolling state + expected-behavior baseline
    engine/
      featurize.py          # L1
      structural.py         # L2 poset matcher + SQL templates
      scoring.py            # L3 calibrated model + attribution
      constraints.py        # honesty rules
      causal.py             # lead-lag / Granger edge testing
    predict/
      label.py  regimes.py  calibrate.py  backtest.py
    outputs/
      cards.py  scanner.py  postmortem.py  explain.py   # deterministic + Claude polish
    storage/                # DuckDB schema, migrations, queries
    schedule/               # APScheduler jobs; batch + intraday loop
    api/                    # FastAPI app + static dashboard
    cli.py                  # typer entrypoints
  data/                     # meridian.duckdb (gitignored)
  tests/                    # pytest + golden fixtures
  pyproject.toml
```

---

## 15. Explanation layer design (deterministic structured cards only — no LLM)

**Decision: structured cards only. No LLM anywhere in the build.**

The card is rendered entirely by **Jinja templates straight from the structured evidence object** — drivers, weights, residual, invalidation, timeline. Implications, by design:
- **Cannot hallucinate.** It can only print fields that exist in the evidence graph; it is structurally incapable of inventing a reason. This is the strongest possible expression of the "explanation may assert nothing absent from the evidence" rule.
- **Fully offline & zero dependency.** No API key, no model server, no network call, no per-run cost. Trivially fast and reproducible.
- **Deterministic & testable.** Identical inputs always yield identical cards, so the renderer is covered by golden-file tests like the rest of the engine.
- **Trade-off accepted:** prose reads more templated/terse than a generative summary. For a trust-first tool that is a feature, not a defect — the card's credibility comes from the evidence and the residual, not from fluent narration.

Template design guidance: invest in well-structured templates (clear driver lines, the partial-order timeline, explicit residual %, the invalidation line) and a small library of phrase fragments keyed to confidence tier and pattern type, so the output is readable without ever generating free text. The structured card is the single source of truth and is always stored as JSON alongside its rendered form.

---

## 16. CLAUDE.md (drop this in the repo root)

```markdown
# Meridian — agent build instructions

You are building a local market event-correlation + prediction engine.
Read ROADMAP.md fully before each phase. Build ONE phase at a time; stop
at each phase's acceptance criteria and report how to run it.

Hard rules (enforce in code, never violate):
- Every event has event_time AND ingest_time; never trust arrival order.
- Pattern matches return graded completeness [0,1], never booleans.
- Every output reports an unexplained residual and an invalidation line.
- Causal edges must pass a statistical lead-lag test before being trusted.
- The explanation layer is deterministic Jinja templates ONLY — no LLM, no API calls. It may only print fields present in the evidence object.
- Free data sources first; Robinhood MCP second; paid only where ROADMAP flags it.
- All thresholds live in Layer 1 featurization, never in pattern definitions.

Stack: Python 3.11+, DuckDB, Polars, scikit-learn, statsmodels, FastAPI, typer,
APScheduler. One .duckdb file in data/. Keep everything runnable locally with no
external services beyond data feeds.

Testing: every engine layer gets pytest golden-file tests. The engine is
deterministic given fixed inputs — tests must pin behavior.

After each phase: print the exact run command and what the user will see.
```

---

## 17. Phased roadmap (each phase = a runnable daily increment)

### Phase 0 — Scaffold & local harness
**Goal:** empty repo → runnable skeleton.
**Build:** repo structure (Sec 14), `pyproject.toml`, `config.yaml`, DuckDB schema + migrations (Sec 8), `cli.py` with `meridian init`, `CLAUDE.md`, pytest harness.
**Acceptance:** `meridian init` creates `data/meridian.duckdb` with all tables; `pytest` green.
**Run:** `meridian init && meridian status`

### Phase 1 — Ingestion & normalization (free + Robinhood)
**Goal:** all *non-options* drivers flowing into typed events.
**Build:** `Adapter` base; adapters for yfinance (price/volume/ETFs/index), FRED (macro), EDGAR (filings), news RSS, earnings; normalization to the Sec 7 schema with dual timestamps; universe loader (S&P500 ∪ Nasdaq-100).
**Acceptance:** one historical day for the full universe lands as `normalized_events`; counts per family printed; clock alignment verified.
**Run:** `meridian ingest --date 2026-06-26`

### Phase 2 — State builder + featurization (L1)
**Goal:** graded events + expected-behavior baseline.
**Build:** rolling 1m/5m/EOD ticker/sector/liquidity state; regime tagger (Sec 12.2); expected-behavior (beta+macro) model; L1 abnormality grading (percentile/z-score vs own regime).
**Acceptance:** every event gets `abnormality` + `regime_tags`; `expected_behavior_1m` populated; residual denominator computable.
**Run:** `meridian featurize --date 2026-06-26`

### Phase 3 — Structural matching (L2) + first 3 patterns
**Goal:** poset graph + partial matches with no options dependency.
**Build:** poset operators (precedes/concurrent/independent/contradicts) as SQL window templates + matcher; `event_edges` writing; patterns: **price-before-news, options-led(price-only proxy), sector-sympathy**; completeness scoring.
**Acceptance:** patterns fire on historical days with completeness scores; edges audited with `rule_id`.
**Run:** `meridian match --date 2026-06-26`

### Phase 4 — Honesty layer + deterministic cards + EOD postmortem
**Goal:** first usable daily output.
**Build:** constraint engine (Sec 11); residual computation; invalidation generator; L3 scoring (transparent weighted model to start); deterministic card renderer; scanner; EOD postmortem; APScheduler post-close job.
**Acceptance:** running post-close produces a postmortem + ranked scanner with residual + invalidation on every card. **This is the first "run it daily" milestone.**
**Run:** `meridian postmortem --date 2026-06-26` / scheduled at 16:30 ET

### Phase 5 — Options / dealer-positioning layer + gamma squeeze (differentiator)
**Goal:** the wedge the competitor lacks.
**Build:** Robinhood option-chain adapter; local greeks; **GEX proxy** from chain OI+gamma (`gex_surface`); events ShortGamma/GammaFlip/CallWall/PutWall/SpotIntoStrike/IVExpansion; **gamma-squeeze pattern**; mechanical-vs-informational classifier.
**Acceptance:** gamma-squeeze fires with completeness; cards show mechanical driver + late-confirmation demotion of headlines.
**Run:** `meridian match --patterns gamma_squeeze --date 2026-06-26`
**Paid flag:** if proxy fidelity is weak in backtest, add ThetaData later (optional).

### Phase 6 — Predictive engine
**Goal:** event correlation → calibrated forward odds.
**Build:** forward-return labeling + MFE/MAE; regime conditioning; per-pattern×regime forward-distribution model; walk-forward calibration + reliability curves; honest paper backtest (`paper_trades`); causal-link testing (Granger/transfer-entropy gate, Sec 12.4).
**Acceptance:** each fired pattern shows forward odds + hit-rate + decay + reliability; edges carry test stats; backtest reports win-rate with residual attached.
**Run:** `meridian backtest --pattern gamma_squeeze` / `meridian calibrate`

### Phase 7 — Automation (both run modes) + intraday loop + scanner UI
**Goal:** hands-off daily operation, intraday switched on.
**Build:** pre-market scan job (~8:30 ET); intraday polling loop (1–5 min) emitting live cards; FastAPI + single-page dashboard (cards, scanner, postmortem); alerting hooks.
**Acceptance:** a full trading day runs unattended: pre-market scan, intraday live cards, post-close postmortem, all viewable in the browser dashboard.
**Run:** `meridian serve` + `meridian schedule --mode both`

### Phase 8 — Card template polish + packaging
**Goal:** readable deterministic cards + easy daily launch.
**Build:** finalize Jinja card/scanner/postmortem templates (Sec 15) — phrase fragments keyed to confidence tier and pattern type, clean partial-order timeline, explicit residual % and invalidation line; golden-file tests pinning rendered output; one-command launcher + launchd/cron install; docs. No LLM.
**Acceptance:** cards are readable and fully deterministic (same input → byte-identical card, verified by golden test); `meridian install-daily` sets up scheduled local runs.
**Run:** `meridian install-daily`

---

## 18. Testing & validation strategy

- **Deterministic golden tests:** fixed input fixtures → pinned event/pattern/score outputs for every engine layer.
- **No-lookahead audit:** automated check that no feature at time *t* uses data with `event_time > t`.
- **Constraint tests:** each honesty rule has a case that must be blocked/downgraded.
- **Calibration tests:** reliability curves within tolerance on holdout; alert on drift.
- **Residual invariant:** attribution + residual = 100% on every output (enforced in code + test).
- **Causal-gate test:** known spurious-correlation fixtures must NOT produce trusted causal edges.
- Recommend a final-phase **subagent review** of the predictive engine for lookahead bias and overfitting before relying on any forward number.

---

## 19. Risks & guardrails (carried into code, not just docs)

| Risk | Control in build |
|---|---|
| False causality | probabilistic language; lineage on every edge; statistical causal gate |
| Feed latency inverting order | dual timestamps; data-quality scoring; downgrade on missing feeds |
| Options misclassification (hedge vs directional) | never label bullish without corroboration; mechanical-vs-informational classifier |
| News coverage gaps | lower confidence when source coverage incomplete |
| Explanation overreach | explanation layer summarizes only the evidence object; diff-test for added claims |
| Overfitting patterns | walk-forward; regime-aware baselines; decay analysis |
| Reflexivity / pareidolia | residual reporting; conservative scoring; causal gate |
| Mistaken for advice | positioned as research/intelligence; "not investment advice" on every surface |

**Claims language — use / avoid:** "most supported explanation" not "the cause was"; "appears to be late confirmation" not "definitely did not matter"; "may have contributed" not "knew something"; "historical analogs showed X" not "this will work."

---

## 20. Definition of done (the everyday-use bar)

Every trading day, with one local install and no manual steps:
1. **Pre-market:** scan of overnight/early drivers across S&P500 ∪ Nasdaq-100.
2. **Intraday:** live "why is it moving" cards on demand, each with ranked drivers, forward odds, unexplained residual, and an invalidation line.
3. **Post-close:** a postmortem of the session's mechanical and price-before-news moves, contagion chains, and which patterns worked/failed.
4. Everything auditable back to source events; nothing asserted beyond the evidence; the share it cannot explain stated out loud.

That is the product: an honest, local, daily event-correlation engine with a calibrated predictive layer — built on the Rapide/CEP model, differentiated on graded matching, dealer-positioning, and real-time honesty.
```
