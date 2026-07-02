#!/usr/bin/env python3
"""Validate feed/meridian-latest.json BEFORE committing/publishing it.

Stdlib only — runs anywhere (pre-commit hook, cron wrapper, CI):

    python3 scripts/validate_feed.py [path] [--expect-date YYYY-MM-DD]

Checks:
  1. required top-level keys are present;
  2. meridian_date == the prior US trading day (Mon-Fri; pass --expect-date to
     override around market holidays);
  3. every counts.* entry matches the length of its published array;
  4. no flow candidate carries |move_pct| > 25% (corporate-action artifacts must be
     routed to suspect_corporate_action, never shipped as flow).

Exit 0 + "[ok]" when clean; exit 1 with a clear message on the FIRST failure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

DEFAULT_PATH = "feed/meridian-latest.json"
MAX_FLOW_MOVE = 0.25

REQUIRED_KEYS = [
    "source",
    "meridian_date",
    "generated_at_utc",
    "regime",
    "counts",
    "gamma_squeeze",
    "flow_candidates",
    "price_before_news",
    "sympathy_beta_deprioritize",
]

# counts key -> top-level array it must agree with
COUNT_TO_ARRAY = {
    "flow_candidates": "flow_candidates",
    "gamma_squeeze": "gamma_squeeze",
    "price_before_news": "price_before_news",
    "sympathy_beta": "sympathy_beta_deprioritize",
    "suspect_corporate_action": "suspect_corporate_action",
    "sympathy_low_confidence": "sympathy_low_confidence",
}


def fail(msg: str) -> None:
    print(f"[fail] {msg}", file=sys.stderr)
    sys.exit(1)


def prior_trading_day(today: dt.date) -> dt.date:
    """The most recent weekday strictly before `today` (holidays: use --expect-date)."""
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= dt.timedelta(days=1)
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate the Meridian feed JSON before commit.")
    ap.add_argument("path", nargs="?", default=DEFAULT_PATH,
                    help=f"feed path (default: {DEFAULT_PATH})")
    ap.add_argument("--expect-date", default=None, metavar="YYYY-MM-DD",
                    help="expected meridian_date override (e.g. around market holidays)")
    args = ap.parse_args()

    try:
        with open(args.path) as fh:
            feed = json.load(fh)
    except FileNotFoundError:
        fail(f"feed file not found: {args.path}")
    except json.JSONDecodeError as exc:
        fail(f"feed is not valid JSON ({args.path}): {exc}")
    if not isinstance(feed, dict):
        fail(f"feed top level must be a JSON object, got {type(feed).__name__}")

    # 1. required keys
    missing = [k for k in REQUIRED_KEYS if k not in feed]
    if missing:
        fail(f"missing required top-level key(s): {', '.join(missing)}")

    # 2. meridian_date == prior US trading day (or the explicit override)
    try:
        got = dt.date.fromisoformat(str(feed["meridian_date"]))
    except ValueError:
        fail(f"meridian_date is not a valid YYYY-MM-DD date: {feed['meridian_date']!r}")
    if args.expect_date:
        try:
            expected = dt.date.fromisoformat(args.expect_date)
        except ValueError:
            fail(f"--expect-date is not a valid YYYY-MM-DD date: {args.expect_date!r}")
    else:
        expected = prior_trading_day(dt.date.today())
    if got != expected:
        fail(f"meridian_date is {got}, expected prior trading day {expected} "
             f"(stale feed? holiday? pass --expect-date to override)")

    # 3. counts.* must match the published array lengths
    counts = feed["counts"]
    if not isinstance(counts, dict):
        fail(f"counts must be an object, got {type(counts).__name__}")
    for ck, ak in COUNT_TO_ARRAY.items():
        if ck not in counts:
            continue
        arr = feed.get(ak)
        if arr is None:
            fail(f"counts.{ck}={counts[ck]} but array '{ak}' is missing")
        if not isinstance(arr, list):
            fail(f"'{ak}' must be an array, got {type(arr).__name__}")
        if counts[ck] != len(arr):
            fail(f"counts.{ck}={counts[ck]} disagrees with len({ak})={len(arr)}")
    unmapped = [k for k in counts if k not in COUNT_TO_ARRAY]
    if unmapped:
        fail(f"counts key(s) with no known array to check: {', '.join(sorted(unmapped))}")

    # 4. no flow candidate with |move| > 25% (corporate-action artifacts)
    for i, row in enumerate(feed["flow_candidates"]):
        mv = row.get("move_pct") if isinstance(row, dict) else None
        if isinstance(mv, (int, float)) and abs(mv) > MAX_FLOW_MOVE:
            fail(f"flow_candidates[{i}] ({row.get('ticker', '?')}) has |move_pct|="
                 f"{abs(mv):.2%} > {MAX_FLOW_MOVE:.0%} — likely split/spinoff artifact; "
                 f"belongs in suspect_corporate_action")

    print(f"[ok] {args.path}: meridian_date={got}, counts match arrays, "
          f"no flow candidate beyond ±{MAX_FLOW_MOVE:.0%}")


if __name__ == "__main__":
    main()
