#!/usr/bin/env python3
"""
helm_signalstack.py — deterministic free-data signal stack for Helm.

Computes the three signal upgrades and writes them to helm-signal-state.json so
every scheduled Helm run consumes IDENTICAL, code-verified numbers instead of
re-deriving them in prose:

  1. Universe BREADTH        -> % of universe above session VWAP / rising 20-DMA -> posture
  2. Time-normalized RVOL    -> today's cumulative volume vs same-time-of-day profile
  3. IV-RANK + EXPECTED MOVE -> ATM IV rank over rolling history; EM = S*IV*sqrt(DTE/365)

Free data only (Yahoo public endpoints), Python stdlib only — runs anywhere Helm
runs (including cloud) with no pip installs and no broker dependency.

STATE FILES (persisted alongside Helm's other memory; same dir as this script
unless --state-dir is given):
  helm-iv-history.json   { "NVDA": [{"d":"2026-06-29","iv":0.42}, ...], ... }  (rolling 252)
  helm-volprofile.json   { "NVDA": {"sessions": 14, "bars": {"13:35": 18234.5, ...}}, ... }
  helm-signal-state.json (OUTPUT) — read by Helm each run; schema in build_state()

MODES
  python3 helm_signalstack.py                 # intraday: compute + write helm-signal-state.json
  python3 helm_signalstack.py --update-history # EOD (4:10 run): append IV history + update vol profile
  python3 helm_signalstack.py --symbols NVDA,AMD   # limit universe (testing)

WARM-UP (never blocks trading; Helm falls back to base rules and logs "warming up"):
  - IV-rank needs >= 30 days of history per symbol, else iv_rank = null.
  - RVOL profile needs >= 10 sessions per symbol, else rvol_basis = "intraday_fallback"
    (current cumulative vs a flat same-bar-count expectation).

v1.1 (2026-07-01 audit fixes):
  - et_bucket is now DST-correct via zoneinfo (was hardcoded UTC-4; EST months
    shifted every volume bucket by one hour and corrupted RVOL).
    NOTE: delete helm-volprofile.json once after deploying this fix so the
    profile rebuilds with correct bucket labels (mixed old/new labels otherwise).
  - Coverage floor: if <70% of the universe returned data, breadth.posture is
    forced to "unknown" (a partial Yahoo outage can no longer fabricate a regime).
  - Session freshness: top-level "intraday_session" reports the ET date of the
    bars used and whether they are live. At the 8:30 pre-market run, vwap/orb/
    rvol describe the PRIOR session — Helm must treat them as context, not live state.
"""

import argparse
import http.cookiejar
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 helm-signalstack/1.1"
HERE = os.path.dirname(os.path.abspath(__file__))
ET_ZONE = ZoneInfo("America/New_York")

# Shared Yahoo session: cookie jar + crumb. Yahoo's finance endpoints now
# return 401 without a consent cookie and (for v7 options) a crumb token.
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
_OPENER.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
_CRUMB = None


def _yahoo_bootstrap():
    global _CRUMB
    if _CRUMB is not None:
        return _CRUMB
    for seed in ("https://fc.yahoo.com/", "https://finance.yahoo.com/"):
        try:
            _OPENER.open(seed, timeout=15).read()
        except Exception:  # noqa: BLE001
            pass  # these often 404 but still set the cookie
    try:
        c = _OPENER.open(
            "https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=15
        ).read().decode("utf-8").strip()
        _CRUMB = c if c and "<" not in c else ""
    except Exception:  # noqa: BLE001
        _CRUMB = ""
    return _CRUMB


def with_crumb(url):
    crumb = _yahoo_bootstrap()
    if not crumb:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}crumb={urllib.parse.quote(crumb)}"

UNIVERSE = [
    "NVDA", "AMD", "AVGO", "MRVL", "TSM", "MU", "LRCX", "AAPL", "MSFT", "META",
    "AMZN", "GOOGL", "NFLX", "TSLA", "PLTR", "ORCL", "ANET", "ASML", "OKTA",
    "NET", "VST", "SHOP", "NOW", "SPY", "QQQ",
]

IV_HISTORY_MIN_DAYS = 30
IV_HISTORY_MAX_DAYS = 252
VOLPROFILE_MIN_SESSIONS = 10
COVERAGE_FLOOR = 0.70          # <70% of universe with data -> posture "unknown"
LIVE_BAR_MAX_AGE_SEC = 20 * 60  # last bar older than this -> not a live session


# --------------------------------------------------------------------------- IO
def _get_json(url, tries=3, pause=0.6):
    last = None
    for _ in range(tries):
        try:
            with _OPENER.open(with_crumb(url), timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            globals()["_CRUMB"] = None  # crumb may have expired; refetch next call
            time.sleep(pause)
    raise last


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


# ----------------------------------------------------------------- market data
def chart(symbol, interval, rng):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={rng}")
    j = _get_json(url)
    res = j["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i],
                         q["close"][i], q["volume"][i])
        if None in (o, h, l, c) or v is None:
            continue
        rows.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
    meta = res.get("meta", {})
    return rows, meta


def et_bucket(epoch):
    """Map a UTC epoch to an ET 'HH:MM' 5-min bucket label.

    v1.1: DST-correct via zoneinfo. The previous version hardcoded UTC-4,
    which shifted every bucket by one hour during EST months (Nov-Mar) and
    corrupted the time-normalized RVOL profile."""
    return datetime.fromtimestamp(epoch, tz=ET_ZONE).strftime("%H:%M")


def et_date(epoch):
    """ET calendar date (YYYY-MM-DD) for a UTC epoch."""
    return datetime.fromtimestamp(epoch, tz=ET_ZONE).strftime("%Y-%m-%d")


def session_vwap(bars):
    num = den = 0.0
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        num += tp * b["v"]
        den += b["v"]
    return (num / den) if den else None


def opening_range(bars, n=6):
    first = bars[:n]
    if not first:
        return None, None
    return max(b["h"] for b in first), min(b["l"] for b in first)


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def atr14(daily):
    if len(daily) < 15:
        return None
    trs = []
    for i in range(1, len(daily)):
        h, l, pc = daily[i]["h"], daily[i]["l"], daily[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / 14.0


def atm_iv(symbol):
    """ATM implied vol from a 2-9 DTE expiration (matches Helm's trade horizon;
    skips 0DTE whose IV reads are unreliable). Returns (iv, spot, expiry_epoch).
    IV below 0.05 is treated as a bad read and returned as None."""
    base = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
    j = _get_json(base)
    oc = j["optionChain"]["result"][0]
    spot = oc.get("quote", {}).get("regularMarketPrice")
    if spot is None:
        return None, spot, None

    now = time.time()
    exps = oc.get("expirationDates") or []
    nearest = (oc.get("options") or [{}])[0].get("expirationDate")
    # Prefer the first expiry >= 2 DTE (ideally <= 10 DTE); else fall back to nearest.
    target = None
    for e in sorted(exps):
        dte = (e - now) / 86400.0
        if dte >= 2:
            target = e
            if dte <= 10:
                break
    opt = (oc.get("options") or [{}])[0]
    exp = nearest
    if target and target != nearest:
        j2 = _get_json(f"{base}?date={int(target)}")
        opt = (j2["optionChain"]["result"][0].get("options") or [{}])[0]
        exp = opt.get("expirationDate")

    def nearest_iv(legs):
        legs = [x for x in legs if x.get("impliedVolatility")]
        if not legs:
            return None
        return min(legs, key=lambda x: abs(x["strike"] - spot)).get("impliedVolatility")

    ivs = [x for x in (nearest_iv(opt.get("calls", [])),
                       nearest_iv(opt.get("puts", []))) if x]
    iv = (sum(ivs) / len(ivs)) if ivs else None
    if iv is not None and iv < 0.05:   # implausible for a liquid mega-cap; bad read
        iv = None
    return iv, spot, exp


def expected_move(spot, iv, dte):
    if not spot or not iv or dte <= 0:
        return None
    return round(spot * iv * math.sqrt(dte / 365.0), 2)


def iv_rank(history):
    ivs = [h["iv"] for h in history]
    if len(ivs) < IV_HISTORY_MIN_DAYS:
        return None, None
    lo, hi = min(ivs), max(ivs)
    cur = ivs[-1]
    rank = None if hi == lo else round((cur - lo) / (hi - lo), 3)
    pct = round(sum(1 for x in ivs if x <= cur) / len(ivs), 3)
    return rank, pct


def rvol_today(symbol, intraday, profile):
    """Today's cumulative volume vs the same-time-of-day expected cumulative."""
    if not intraday:
        return None, "no_data"
    now_label = et_bucket(intraday[-1]["t"])
    today_cum = sum(b["v"] for b in intraday)
    sym_prof = profile.get(symbol, {})
    bars = sym_prof.get("bars", {})
    sessions = sym_prof.get("sessions", 0)
    if sessions >= VOLPROFILE_MIN_SESSIONS and bars:
        labels = sorted(bars.keys())
        expected_cum = sum(bars[lb] for lb in labels if lb <= now_label)
        if expected_cum > 0:
            return round(today_cum / expected_cum, 2), "profile"
    # Fallback (no profile yet): latest 5-min bar volume vs the average of the
    # prior up-to-12 completed bars today — a real intraday relative-volume read.
    if len(intraday) >= 4:
        last = intraday[-1]["v"]
        prior = [b["v"] for b in intraday[-13:-1]]
        avg = sum(prior) / len(prior) if prior else 0
        return (round(last / avg, 2) if avg else None), "intraday_fallback"
    return None, "intraday_fallback"


# ------------------------------------------------------------------- assembly
def build_state(symbols, state_dir):
    iv_hist = load_json(os.path.join(state_dir, "helm-iv-history.json"), {})
    profile = load_json(os.path.join(state_dir, "helm-volprofile.json"), {})

    out = {}
    above_vwap = above_ma = counted = 0
    warming_iv = warming_rvol = False
    last_bar_epoch = 0

    for s in symbols:
        try:
            intraday, _ = chart(s, "5m", "1d")
            daily, _ = chart(s, "1d", "6mo")
            closes = [d["c"] for d in daily]
            price = intraday[-1]["c"] if intraday else (closes[-1] if closes else None)
            if intraday:
                last_bar_epoch = max(last_bar_epoch, intraday[-1]["t"])
            vwap = session_vwap(intraday)
            ma20, ma50 = sma(closes, 20), sma(closes, 50)
            ma20_prev = sma(closes[:-1], 20) if len(closes) > 20 else None
            rising20 = (ma20 is not None and ma20_prev is not None and ma20 > ma20_prev)
            orb_h, orb_l = opening_range(intraday)
            atr = atr14(daily)
            iv, spot, _ = atm_iv(s)
            rank, pct = iv_rank(iv_hist.get(s, []) + ([{"d": "today", "iv": iv}] if iv else []))
            rvol, rbasis = rvol_today(s, intraday, profile)

            a_vwap = bool(price and vwap and price > vwap)
            a_ma = bool(price and ma20 and rising20 and price > ma20)
            if price is not None:
                counted += 1
                above_vwap += int(a_vwap)
                above_ma += int(a_ma)
            if rank is None:
                warming_iv = True
            if rbasis != "profile":
                warming_rvol = True

            out[s] = {
                "price": round(price, 2) if price else None,
                "vwap": round(vwap, 2) if vwap else None,
                "above_vwap": a_vwap,
                "ma20": round(ma20, 2) if ma20 else None,
                "ma50": round(ma50, 2) if ma50 else None,
                "above_rising_20dma": a_ma,
                "atr14": round(atr, 2) if atr else None,
                "atr_pct": round(atr / price * 100, 2) if (atr and price) else None,
                "orb_high": round(orb_h, 2) if orb_h else None,
                "orb_low": round(orb_l, 2) if orb_l else None,
                "rvol": rvol,
                "rvol_basis": rbasis,
                "atm_iv": round(iv, 4) if iv else None,
                "iv_rank": rank,
                "iv_pct": pct,
                "expected_move": {
                    "1d": expected_move(spot or price, iv, 1),
                    "3d": expected_move(spot or price, iv, 3),
                    "5d": expected_move(spot or price, iv, 5),
                },
            }
        except Exception as e:  # noqa: BLE001
            out[s] = {"error": str(e)}

    coverage = round(counted / len(symbols), 2) if symbols else 0.0
    avp = round(above_vwap / counted * 100, 1) if counted else None
    amp = round(above_ma / counted * 100, 1) if counted else None

    # v1.1 coverage floor: a partial Yahoo outage must not fabricate a regime.
    posture = "unknown"
    if avp is not None and coverage >= COVERAGE_FLOOR:
        posture = "risk_on" if avp > 60 else "risk_off" if avp < 40 else "mixed"

    # v1.1 session freshness: are these bars from a live session right now,
    # or from the prior session (e.g. the 8:30 pre-market run)?
    bar_age = (time.time() - last_bar_epoch) if last_bar_epoch else None
    intraday_session = {
        "session_date_et": et_date(last_bar_epoch) if last_bar_epoch else None,
        "last_bar_age_min": round(bar_age / 60.0, 1) if bar_age is not None else None,
        "is_live_session": bool(bar_age is not None and bar_age <= LIVE_BAR_MAX_AGE_SEC),
    }

    return {
        "source": "helm_signalstack",
        "version": "1.1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_count": len(symbols),
        "intraday_session": intraday_session,
        "breadth": {
            "above_vwap_pct": avp,
            "above_rising_20dma_pct": amp,
            "n": counted,
            "coverage": coverage,
            "posture": posture,
        },
        "warming_up": {"iv_rank": warming_iv, "rvol_profile": warming_rvol},
        "notes": (
            "Free-data signal stack. breadth.posture: >60% above VWAP=risk_on, "
            "<40%=risk_off, else mixed; posture is 'unknown' when coverage<0.70 "
            "(partial data must not set a regime). intraday_session.is_live_session "
            "false => vwap/orb/rvol describe the PRIOR session (e.g. 8:30 pre-market "
            "run) — treat as context, not live state. iv_rank null => <30d history "
            "(skip IV-rank gate, keep expected_move). rvol_basis 'intraday_fallback' "
            "=> <10 sessions of profile. expected_move = spot*IV*sqrt(DTE/365). "
            "This is signal INPUT only; Helm still requires a trigger and obeys RISK LIMITS."
        ),
        "symbols": out,
    }


def update_history(symbols, state_dir):
    """EOD: append today's ATM IV (rolling 252) and fold today's intraday volume
    into the per-bucket profile (running mean)."""
    iv_path = os.path.join(state_dir, "helm-iv-history.json")
    vp_path = os.path.join(state_dir, "helm-volprofile.json")
    iv_hist = load_json(iv_path, {})
    profile = load_json(vp_path, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for s in symbols:
        try:
            iv, _, _ = atm_iv(s)
            if iv:
                hist = [h for h in iv_hist.get(s, []) if h.get("d") != today]
                hist.append({"d": today, "iv": round(iv, 4)})
                iv_hist[s] = hist[-IV_HISTORY_MAX_DAYS:]
            intraday, _ = chart(s, "5m", "1d")
            sp = profile.get(s, {"sessions": 0, "bars": {}})
            n = sp["sessions"]
            for b in intraday:
                lb = et_bucket(b["t"])
                prev = sp["bars"].get(lb)
                sp["bars"][lb] = b["v"] if prev is None else (prev * n + b["v"]) / (n + 1)
            sp["sessions"] = n + 1
            profile[s] = sp
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {s}: {e}", file=sys.stderr)

    save_json(iv_path, iv_hist)
    save_json(vp_path, profile)
    print(f"[ok] updated history for {len(symbols)} symbols ({today})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma list to override the universe (testing)")
    ap.add_argument("--update-history", action="store_true", help="EOD maintenance mode")
    ap.add_argument("--state-dir", default=HERE, help="dir for state files (default: script dir)")
    ap.add_argument("--out", default=None, help="output path for signal state")
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")]
               if args.symbols else UNIVERSE)

    if args.update_history:
        update_history(symbols, args.state_dir)
        return

    state = build_state(symbols, args.state_dir)
    out = args.out or os.path.join(args.state_dir, "helm-signal-state.json")
    save_json(out, state)
    b = state["breadth"]
    print(f"[ok] wrote {out}: posture={b['posture']} "
          f"above_vwap={b['above_vwap_pct']}% n={b['n']} "
          f"coverage={b['coverage']} live={state['intraday_session']['is_live_session']} "
          f"warming={state['warming_up']}")


if __name__ == "__main__":
    main()
