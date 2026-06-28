# Meridian — Evaluator Feedback

Copy this file, fill it in, and send it back. Be specific; cite tickers/dates/commands so
findings are reproducible. Nothing here is sent automatically — you share it manually.

## 0. Environment
- Commit / tag evaluated:
- OS / Python version:
- Install path used (`pip install -e ".[...]"`):
- Did `pytest -q` pass? (paste the summary line):
- Did `ruff check meridian` pass?

## 1. First-run experience
- Did `meridian demo` complete offline in one command? (Y/N; time taken)
- Did `meridian serve` render cards/scanner/postmortem? (Y/N)
- Anything confusing in setup or docs?

## 2. Bring-your-own-data
- Route used: CSV example / custom adapter
- Data shape (families, # tickers, date range):
- Is your data point-in-time correct (no survivorship, as-first-reported)? (Y/N + caveats)
- Were `event_time`s real publish/occurrence times in UTC? (Y/N)
- Any friction implementing the adapter? What was unclear in `docs/BRING_YOUR_OWN_DATA.md`?

## 3. Correctness checks (rate each: PASS / FAIL / N-A + notes)
- [ ] Every card shows an **unexplained residual** and an **invalidation line**
- [ ] `attribution + residual == 1.0`, residual never 0
- [ ] **No-lookahead**: grading at *t* never uses `event_time > t`
- [ ] Pattern completeness is graded in [0,1], never boolean
- [ ] Every row records a `data_source` (run `meridian data-report`)
- [ ] Deterministic: same input → identical cards
- [ ] Causal `precedes` edges only when the lead-lag test passes (else downgraded)

## 4. Predictive quality (if you ran backtest/calibrate)
- Patterns tested + horizons:
- Hit-rate / decay sensible vs your expectations?
- Reliability curves (predicted vs realized) — monotonic? well-calibrated?
- Was the unexplained residual reported alongside every backtest number?

## 5. Usefulness (the subjective but crucial part)
- Were the "why is it moving" cards **informative**? Example of a good one:
- Example of a misleading or low-value one:
- Did the mechanical-vs-informational / late-confirmation framing help?
- What would make a card actually actionable for you?

## 6. Bugs / surprises
- Repro steps + expected vs actual (one per finding):

## 7. Top 3 changes you'd prioritize
1.
2.
3.

## 8. Overall
- Would this be useful to you as a daily tool? (1–5 + why)
- Biggest risk or thing you don't trust yet:
