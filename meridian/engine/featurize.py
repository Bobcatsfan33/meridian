"""Layer-1 featurization (Phase 2): raw normalized events -> graded events.

Each event for the target trading day gets a continuous `abnormality` in [0,1] graded
against THIS name's own trailing regime baseline (percentile of the move's magnitude),
plus the day's `regime_tags`. THE ONLY layer where thresholds live (all from config).
No-lookahead: the trailing baseline for date D uses only state rows with ts < close(D).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from . import featurize_grade as grade
from ..config import Config
from ..ingest.clock import market_close_utc

CONTINUOUS_FAMILIES = {"price_volume", "sector_peer", "macro"}
OPTIONS_FAMILIES = {"dealer_pos", "options_flow"}
EQUITY_FLOW_FAMILY = "equity_flow"
DISCRETE_FAMILIES = {"filing", "news", "earnings", "liquidity", "attention"}


@dataclass
class FeaturizeSummary:
    target_date: dt.date
    n_events: int = 0
    n_graded: int = 0
    n_insufficient_history: int = 0
    family_counts: dict[str, int] = field(default_factory=dict)
    abnormality_mean: float = float("nan")
    regime_label: str = ""
    regime_tags: list[str] = field(default_factory=list)


def featurize(con, cfg: Config, target_date: dt.date) -> FeaturizeSummary:
    f = cfg.featurization
    min_hist = int(f.get("min_history_days", 20))
    win = int(f.get("baseline_window_days", 60))
    insuff = float(f.get("insufficient_history_abnormality", 0.5))
    priors = f.get("discrete_priors", {}) or {}
    opt_cfg = f.get("options", {}) or {}
    close_ts = market_close_utc(target_date).replace(tzinfo=None)

    regime_tags, regime_label = _regime(con, target_date)
    events = _day_events(con, target_date)
    abn_eb = _abnormal_ret_map(con, close_ts)

    graded_rows: list[tuple] = []
    n_insuff = 0
    fam_counts: dict[str, int] = {}
    abn_sum, abn_n = 0.0, 0
    for ev in events:
        fam = ev["family"]
        fam_counts[fam] = fam_counts.get(fam, 0) + 1
        if fam in CONTINUOUS_FAMILIES and ev["ticker"]:
            res = grade.grade_continuous(con, ev["ticker"], close_ts, win, min_hist, insuff)
        elif fam in OPTIONS_FAMILIES:
            res = grade.grade_options(ev, opt_cfg)
        elif fam == EQUITY_FLOW_FAMILY and ev["ticker"]:
            res = grade.grade_equity_flow(con, ev, ev["event_time"], win, min_hist, insuff)
        else:
            res = grade.grade_discrete(con, ev, target_date, win, priors)
        if res.insufficient:
            n_insuff += 1
        payload = dict(res.components)
        if ev["ticker"] in abn_eb:
            payload["abnormal_ret"] = abn_eb[ev["ticker"]]  # residual denominator (beta+macro)
        payload["grade_method"] = res.method
        conf = (ev["confidence"] or 0.0) * (0.8 if res.insufficient else 1.0)
        graded_rows.append(
            (ev["event_id"], ev["event_time"], ev["ticker"], ev["event_type"],
             res.abnormality, list(regime_tags), conf, json.dumps(payload, default=str))
        )
        if res.abnormality is not None and not _nan(res.abnormality):
            abn_sum += res.abnormality
            abn_n += 1

    _write_graded(con, [e["event_id"] for e in events], graded_rows)
    return FeaturizeSummary(
        target_date=target_date,
        n_events=len(events),
        n_graded=len(graded_rows),
        n_insufficient_history=n_insuff,
        family_counts=dict(sorted(fam_counts.items())),
        abnormality_mean=(abn_sum / abn_n) if abn_n else float("nan"),
        regime_label=regime_label,
        regime_tags=list(regime_tags),
    )


def _regime(con, target_date) -> tuple[tuple[str, ...], str]:
    row = con.execute(
        "SELECT regime_label, regime_tags FROM regimes_daily WHERE trade_date = ?", [target_date]
    ).fetchone()
    if not row:
        return (), ""
    return tuple(row[1] or []), row[0] or ""


def _day_events(con, target_date) -> list[dict]:
    rows = con.execute(
        "SELECT event_id, event_time, ticker, event_type, family, confidence, payload "
        "FROM normalized_events WHERE CAST(event_time AS DATE) = ? ORDER BY event_time, event_id",
        [target_date],
    ).fetchall()
    out = []
    for eid, et, tk, etype, fam, conf, payload in rows:
        out.append({"event_id": eid, "event_time": et, "ticker": tk, "event_type": etype,
                    "family": fam, "confidence": conf,
                    "payload": json.loads(payload) if payload else {}})
    return out


def _abnormal_ret_map(con, close_ts) -> dict[str, float]:
    rows = con.execute(
        "SELECT ticker, abnormal_ret FROM expected_behavior_1m WHERE ts = ? AND abnormal_ret IS NOT NULL",
        [close_ts],
    ).fetchall()
    return {t: a for t, a in rows}


def _write_graded(con, event_ids: list[str], rows: list[tuple]) -> None:
    if event_ids:
        con.execute(
            "DELETE FROM graded_events WHERE event_id IN (%s)" % ",".join("?" * len(event_ids)),
            event_ids,
        )
    if rows:
        con.executemany(
            "INSERT INTO graded_events (event_id, event_time, ticker, event_type, abnormality, "
            "regime_tags, confidence, payload) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and x != x)
