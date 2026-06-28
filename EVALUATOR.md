# Meridian — Evaluator Guide

Thanks for evaluating Meridian. This guide is for an outside tester who wants to run the
engine on their own historical data and give useful feedback. It is **docs + scaffolding
only** — running an evaluation never changes engine logic.

## What Meridian is
A **local, daily** market event-correlation + prediction engine. It ingests market-event
drivers into a typed event stream (with dual timestamps), grades each event against the
name's *own* regime baseline, matches declarative **graded** patterns over a partial-order
("poset") graph, attributes an abnormal move to drivers with an auditable evidence trail,
and scores **calibrated forward odds** — always reporting the share of the move it *cannot*
explain. The explanation layer is **deterministic Jinja templates only — no LLM anywhere.**

## What Meridian is **not**
- Not a guaranteed predictor, not proof of causality, not automated execution.
- Not investment advice. Every output carries an unexplained residual and an invalidation line.
- Not a black box: every claim maps back to source events with timestamps and a `rule_id`.

## Privacy / local-only guarantee
- Meridian runs **entirely on your machine** against an embedded DuckDB file (`data/meridian.duckdb`).
- The **demo and the evaluation path send nothing anywhere** — no telemetry, no account, no
  network calls when you use `meridian demo` or bring your own offline data.
- The only outbound traffic is the optional **live data adapters you explicitly enable** (yfinance,
  FRED, SEC EDGAR, Yahoo RSS, FINRA, and the opt-in Massive). Disable them and Meridian is air-gapped.
- Your data never leaves the box. Backups are local files under `data/backups/` (gitignored).

## Quickstart (one command, offline, no keys)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + tests; add [data,ml,api] for live feeds/dashboard
meridian demo                    # offline, deterministic end-to-end run on committed fixtures
meridian serve                   # open http://127.0.0.1:8765/ to view the cards/scanner/postmortem
```
`meridian demo` ingests a tiny fixture set, featurizes, matches, builds explanations + a
postmortem, and prints a summary — with no network and no API keys.

## Bring your own data
You can evaluate Meridian on **your** historical events. Two supported routes:
1. **CSV** — map your history to the documented CSV schema and use the worked example adapter
   in [`docs/examples/csv_adapter.py`](docs/examples/csv_adapter.py) (copy it, point it at your file).
2. **Custom adapter** — implement the `Adapter` interface directly. The full interface, the
   canonical event schema (field by field), and the two hard rules are in
   [`docs/BRING_YOUR_OWN_DATA.md`](docs/BRING_YOUR_OWN_DATA.md).

Then run the pipeline on a date you have data for:
```bash
meridian ingest --date YYYY-MM-DD -a <your_adapter>   # or load via the CSV example
meridian featurize --date YYYY-MM-DD
meridian match     --date YYYY-MM-DD
meridian postmortem --date YYYY-MM-DD
```

## What to evaluate (and where it's verifiable)
- **Honesty** — does every card show an unexplained residual + an invalidation line? Does
  `attribution + residual == 1.0` (and residual never 0)?
- **No-lookahead** — does grading at time *t* ever use data with `event_time > t`? (See the
  adversarial guard `tests/test_no_lookahead.py`.)
- **Graded matching** — patterns return completeness in [0,1], never booleans.
- **Provenance** — does every row record its `data_source`? (`meridian data-report`.)
- **Calibration** — are the reliability curves sensible on your data? (`meridian calibrate`.)
- **Determinism** — same input → byte-identical cards (golden tests).
- **Usefulness** — are the readouts, drivers, and invalidation lines actually informative?

When you're done, fill in [`FEEDBACK_TEMPLATE.md`](FEEDBACK_TEMPLATE.md).

## Reproducibility / determinism
The engine is deterministic given fixed inputs; every layer has golden-file tests. `pytest -q`
should be green, `ruff check meridian` clean. If `meridian demo` differs run-to-run, that's a bug
worth reporting.
