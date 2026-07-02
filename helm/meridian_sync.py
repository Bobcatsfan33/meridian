#!/usr/bin/env python3
"""
meridian_sync.py — Mac-side bridge that publishes Meridian's daily EOD digest
to a place cloud-hosted Helm can fetch over HTTPS.

v2 (2026-07-01 audit rewrite)
-----------------------------
The old version built its OWN digest from Meridian's local API, which bypassed
every correctness fix inside Meridian (adjusted prices, proxy_data honesty,
corporate-action gate, sympathy residual filter, options_layer_ran). It also
pushed with zero validation, which is how a fabricated row (HON "-47.16%")
reached the live feed Helm trades against.

New flow:
  1. PREFERRED: read the digest Meridian itself now writes at
     <repo>/feed/meridian-latest.json (produced by run_postclose -> write_digest,
     which contains all audit fixes).
  2. FALLBACK (repo digest missing/stale): build from the local API as before,
     but apply the audit corrections inline (see _build_digest_from_api).
  3. ALWAYS validate before publishing — via <repo>/scripts/validate_feed.py when
     present, plus built-in checks. A digest that fails validation is NEVER pushed.

USAGE
  python3 meridian_sync.py                 # fetch/validate, write ./meridian-latest.json
  python3 meridian_sync.py --date 2026-06-30
  python3 meridian_sync.py --publish       # also git add/commit/push from the repo clone

SCHEDULE (Mac, launchd/cron), after Meridian's EOD and before Helm's 08:30 ET run:
  30 17 * * 1-5  /usr/bin/python3 "/Users/rwallace/Desktop/Agents RH/meridian_sync.py" --publish >> "/Users/rwallace/Desktop/Agents RH/logs/meridian_sync.log" 2>&1

Set MERIDIAN_PUBLISH_REPO to the local clone path of github.com/Bobcatsfan33/meridian
if auto-detection doesn't find it.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
MERIDIAN_BASE = os.environ.get("MERIDIAN_BASE", "http://127.0.0.1:8765")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
LATEST_JSON = os.path.join(OUT_DIR, "meridian-latest.json")

HELM_UNIVERSE = {
    "NVDA", "AMD", "AVGO", "MRVL", "TSM", "MU", "LRCX", "AAPL", "MSFT", "META",
    "AMZN", "GOOGL", "GOOG", "NFLX", "TSLA", "PLTR", "ORCL", "ANET", "ASML",
    "OKTA", "NET", "VST", "SHOP", "NOW", "SPY", "QQQ",
}

FLOW_PATTERNS = {"options_led_proxy", "gamma_squeeze"}
FLOW_TIERS = {"High", "Medium"}
FLOW_RESIDUAL_MAX = 0.50
SYMPATHY_MIN_MOVE = 0.02
SYMPATHY_RESIDUAL_MAX = 0.60   # audit fix: residual >= 0.6 means the sector-beta
                               # decomposition failed to explain the move
SUSPECT_MOVE_ABS = 0.25        # audit fix: |move| > 25% with no catalyst evidence
                               # is quarantined (corporate-action artifact risk)
MAX_PER_LIST = 40

PUBLISH_PATH = "feed/meridian-latest.json"
PUBLISH_BRANCH = "main"
REPO_CANDIDATES = [
    "~/code/meridian", "~/meridian", "~/Documents/meridian", "~/Projects/meridian",
    "~/projects/meridian", "~/dev/meridian", "~/git/meridian", "~/repos/meridian",
    "~/Desktop/meridian",
]


def find_repo() -> Optional[str]:
    """Locate the local meridian clone: env var first, then common paths."""
    env = os.environ.get("MERIDIAN_PUBLISH_REPO", "")
    candidates = ([env] if env else []) + REPO_CANDIDATES
    for c in candidates:
        p = os.path.expanduser(c)
        if p and os.path.isdir(os.path.join(p, ".git")):
            return p
    return None


def _get(path: str):
    url = f"{MERIDIAN_BASE}{path}"
    with urllib.request.urlopen(url, timeout=15) as r:
        body = r.read().decode("utf-8")
    ctype = r.headers.get("Content-Type", "")
    return (json.loads(body) if "application/json" in ctype else body), ctype


def latest_date() -> str:
    dates, _ = _get("/api/dates")
    if not dates:
        raise SystemExit("Meridian returned no dates")
    return sorted(dates, reverse=True)[0]


def expected_trading_day() -> str:
    """Most recent weekday (Mon-Fri) in local time. Holidays aren't modeled — on a
    holiday the date simply won't match, the bridge skips, and Helm's staleness
    guard ignores a non-current feed."""
    d = datetime.now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


# ---------------------------------------------------------------------------
# VALIDATION — nothing is published without passing this
# ---------------------------------------------------------------------------
REQUIRED_KEYS = {"source", "meridian_date", "generated_at_utc", "counts",
                 "flow_candidates", "gamma_squeeze", "sympathy_beta_deprioritize"}


def validate_digest(digest: dict, expect_date: str) -> list:
    """Built-in checks mirroring scripts/validate_feed.py. Returns list of errors."""
    errors = []
    missing = REQUIRED_KEYS - set(digest.keys())
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")
    if digest.get("meridian_date") != expect_date:
        errors.append(f"meridian_date {digest.get('meridian_date')!r} != expected {expect_date!r}")
    counts = digest.get("counts", {})
    for name in ("flow_candidates", "gamma_squeeze", "price_before_news",
                 "sympathy_beta_deprioritize"):
        arr = digest.get(name)
        if isinstance(arr, list) and name in counts and counts[name] != len(arr):
            errors.append(f"counts.{name}={counts[name]} != len(array)={len(arr)}")
    for row in digest.get("flow_candidates", []) or []:
        if abs(row.get("move_pct", 0.0)) > SUSPECT_MOVE_ABS:
            errors.append(
                f"flow candidate {row.get('ticker')} |move| "
                f"{row.get('move_pct'):+.2%} > {SUSPECT_MOVE_ABS:.0%} — possible "
                "corporate-action artifact; must be quarantined, not published")
    return errors


def run_repo_validator(repo: Optional[str], feed_path: str, expect_date: str) -> bool:
    """Run <repo>/scripts/validate_feed.py when available (authoritative). True=pass."""
    if not repo:
        return True
    script = os.path.join(repo, "scripts", "validate_feed.py")
    if not os.path.isfile(script):
        return True
    r = subprocess.run(
        [sys.executable, script, feed_path, "--expect-date", expect_date],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[validate] repo validator FAILED:\n{r.stdout}{r.stderr}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# SOURCE 1 — Meridian's own corrected digest (preferred)
# ---------------------------------------------------------------------------
def load_repo_digest(repo: Optional[str], expect_date: str) -> Optional[dict]:
    if not repo:
        return None
    p = os.path.join(repo, PUBLISH_PATH)
    try:
        with open(p) as f:
            digest = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if digest.get("meridian_date") != expect_date:
        return None
    return digest


# ---------------------------------------------------------------------------
# SOURCE 2 — legacy API build, WITH the audit corrections applied inline
# ---------------------------------------------------------------------------
def parse_regime(postmortem_text: str) -> dict:
    regime, tags, names = None, [], None
    m = re.search(r"Regime:\s*(\S+)\s*tags:\s*([^\n]+)", postmortem_text)
    if m:
        regime = m.group(1).strip()
        tags = [t.strip() for t in re.split(r"[,\s]+", m.group(2)) if t.strip()]
    n = re.search(r"Names explained:\s*(\d+)", postmortem_text)
    if n:
        names = int(n.group(1))
    return {"regime": regime, "tags": tags, "names_explained": names}


def _build_digest_from_api(date: str) -> dict:
    scanner, _ = _get(f"/api/scanner?date={date}")
    postmortem, _ = _get(f"/api/postmortem/{date}")
    regime = parse_regime(postmortem if isinstance(postmortem, str) else "")

    def tag(row):
        row = dict(row)
        row["in_universe"] = row.get("ticker") in HELM_UNIVERSE
        row["move_pct_display"] = round(row.get("move_pct", 0) * 100, 2)
        return row

    flow, gamma, leak, sympathy = [], [], [], []
    suspect, sympathy_low = [], []
    for row in scanner:
        pat = row.get("pattern")
        tier = row.get("tier")
        resid = row.get("residual", 1.0)
        move = abs(row.get("move_pct", 0.0))
        if pat == "gamma_squeeze":
            gamma.append(tag(row))
        if pat in FLOW_PATTERNS and tier in FLOW_TIERS and resid <= FLOW_RESIDUAL_MAX:
            # audit fix: quarantine implausible movers (corporate-action artifacts)
            if move > SUSPECT_MOVE_ABS:
                suspect.append(tag(row))
            else:
                flow.append(tag(row))
        if pat == "price_before_news":
            leak.append(tag(row))
        if pat == "sector_sympathy" and move >= SYMPATHY_MIN_MOVE:
            # audit fix: a row the model couldn't explain isn't sector-beta evidence
            basis = row.get("residual_basis", "return")
            if basis == "return" and resid >= SYMPATHY_RESIDUAL_MAX:
                sympathy_low.append(tag(row))
            else:
                sympathy.append(tag(row))

    tier_rank = {"High": 0, "Medium": 1, "Low": 2}

    def key(r):
        return (
            0 if r.get("in_universe") else 1,
            tier_rank.get(r.get("tier"), 9),
            -(r.get("confidence") or 0),
            r.get("residual", 1.0),
        )

    flow.sort(key=key)
    gamma.sort(key=key)
    sympathy.sort(key=lambda r: (0 if r.get("in_universe") else 1,
                                 -abs(r.get("move_pct", 0))))

    # audit fix: publish truncated arrays and make counts MATCH what's published
    gamma = gamma[:MAX_PER_LIST]
    flow = flow[:MAX_PER_LIST]
    leak = leak[:MAX_PER_LIST]
    sympathy = sympathy[:MAX_PER_LIST]

    return {
        "source": "meridian",
        "source_mode": "api_fallback_v2",   # repo digest preferred; this is the fallback
        "meridian_date": date,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "counts": {
            "flow_candidates": len(flow),
            "gamma_squeeze": len(gamma),
            "price_before_news": len(leak),
            "sympathy_beta_deprioritize": len(sympathy),
        },
        "notes": (
            "EOD attribution of the prior session. Use as pre-market regime + "
            "watchlist overlay ONLY; not an intraday trigger. flow_candidates are a "
            "price/volume PROXY (no options data observed) — treat as momentum "
            "watchlist seeds, never flow confirmation. Lower residual = the labeled "
            "mechanism explains more of the move. Helm must still require its own "
            "intraday trigger and obey all RISK LIMITS."
        ),
        "gamma_squeeze": gamma,
        "flow_candidates": flow,
        "price_before_news": leak,
        "sympathy_beta_deprioritize": sympathy,
        "suspect_corporate_action": suspect[:MAX_PER_LIST],
        "sympathy_low_confidence": sympathy_low[:MAX_PER_LIST],
    }


# ---------------------------------------------------------------------------
# PUBLISH
# ---------------------------------------------------------------------------
def publish(repo: Optional[str], local_path: str) -> None:
    if not repo:
        print("[publish] meridian clone not found (set MERIDIAN_PUBLISH_REPO) — "
              "skipping git publish.")
        return
    dst = os.path.join(repo, PUBLISH_PATH)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(local_path) as f:
        data = f.read()
    with open(dst, "w") as f:
        f.write(data)
    try:
        subprocess.run(["git", "-C", repo, "add", PUBLISH_PATH], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-m",
                        f"helm feed {datetime.now().date()} (validated)"], check=True)
        subprocess.run(["git", "-C", repo, "push", "origin", PUBLISH_BRANCH], check=True)
        print(f"[publish] pushed {PUBLISH_PATH} -> origin/{PUBLISH_BRANCH}")
    except subprocess.CalledProcessError as e:
        print(f"[publish] git step failed (nothing to commit is OK): {e}")


def already_published(repo: Optional[str], date: str) -> bool:
    if not repo:
        return False
    p = os.path.join(repo, PUBLISH_PATH)
    try:
        with open(p) as f:
            return json.load(f).get("meridian_date") == date
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today's trading day)")
    ap.add_argument("--publish", action="store_true", help="git-push the feed after writing")
    ap.add_argument("--out", default=LATEST_JSON, help="output path")
    ap.add_argument("--force", action="store_true",
                    help="proceed even if Meridian's latest date isn't today's trading day")
    args = ap.parse_args()

    repo = find_repo()
    expect = args.date or expected_trading_day()

    # SOURCE 1: the corrected digest Meridian itself writes (has all audit fixes).
    digest = load_repo_digest(repo, expect)
    if digest is not None:
        print(f"[ok] using Meridian's own digest from {repo}/{PUBLISH_PATH} for {expect}")
    else:
        # SOURCE 2: legacy API build with inline corrections.
        try:
            _get("/api/health")
        except Exception as e:  # noqa: BLE001
            print(f"[error] no repo digest for {expect} and Meridian API not reachable "
                  f"at {MERIDIAN_BASE}: {e}", file=sys.stderr)
            sys.exit(2)
        date = args.date or latest_date()
        if not args.date and not args.force and date != expect:
            print(f"[skip] Meridian latest date {date} != expected {expect} — ingest "
                  f"not finished yet; not publishing. Will retry on the next run.")
            return
        digest = _build_digest_from_api(date)
        expect = date

    if args.publish and already_published(repo, expect) and digest.get("source_mode") != "api_fallback_v2":
        print(f"[skip] feed already published for {expect}; nothing to do.")
        return

    with open(args.out, "w") as f:
        json.dump(digest, f, indent=2)
    print(f"[ok] wrote {args.out} for {expect}: "
          f"{digest['counts'].get('flow_candidates', '?')} flow, "
          f"{digest['counts'].get('gamma_squeeze', '?')} gamma, "
          f"regime={(digest.get('regime') or {}).get('regime')}")

    # VALIDATION GATE — a digest that fails is never pushed.
    errors = validate_digest(digest, expect)
    if errors:
        print("[validate] FAILED — NOT publishing:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(3)
    if not run_repo_validator(repo, args.out, expect):
        print("[validate] repo validator rejected the feed — NOT publishing.", file=sys.stderr)
        sys.exit(3)
    print("[validate] passed")

    if args.publish:
        publish(repo, args.out)


if __name__ == "__main__":
    main()
