# Meridian — agent build instructions

You are building a local market event-correlation + prediction engine for stocks.
Read ROADMAP.md fully before each phase. **Phase 0 is already built** (scaffold,
DuckDB schema, universe, config, CLI, tests). Start from Phase 1 and build ONE
phase at a time, stopping at each phase's acceptance criteria.

## Hard rules (enforce in code, never violate)
- Every event has event_time AND ingest_time; never trust arrival order.
- Pattern matches return graded completeness in [0,1], never booleans.
- Every output reports an unexplained residual AND an invalidation line.
- attribution weights + unexplained_residual must sum to 1.0 (enforce + test).
- Causal edges must pass a statistical lead-lag test (p < engine.causal_test_alpha)
  before edge_type is set to `precedes`; otherwise downgrade to concurrent/independent.
- The explanation layer is DETERMINISTIC Jinja templates ONLY — no LLM, no API
  calls. It may only print fields present in the structured evidence object.
- All thresholds live in Layer-1 featurization, never in pattern definitions.
- Data sources: free first (yfinance/EDGAR/FRED/RSS), Robinhood MCP second,
  paid (ThetaData) only where ROADMAP explicitly flags it.

## Stack
Python 3.11+, DuckDB (single file at data/meridian.duckdb), Polars, scikit-learn,
statsmodels, FastAPI + single-page HTML dashboard, typer CLI, APScheduler.
No external services beyond data feeds. Everything runs locally.

## Conventions
- Match the repo layout in ROADMAP.md Section 14. Keep modules small.
- All config flows through meridian/config.py (reads config/config.yaml). No hardcoded paths.
- Adapters implement a common base interface; ship disabled, enable via config.
- Every engine layer gets pytest golden-file tests; the engine is deterministic
  given fixed inputs, so pin behavior.
- Add a no-lookahead audit: no feature at time t may use data with event_time > t.

## After each phase
Run the phase's acceptance check + run command, show me the output, and STOP until
I say continue.
