"""Build + persist move_explanations for a date (one card per ticker, best firing).

Reloads the day's graded events (payloads merged from normalized+graded for readable
timelines), rebinds each chosen firing's pattern roles, builds the evidence object, and
writes move_explanations. Enforces attribution + residual == 1.0 before persisting.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ..config import Config
from ..engine import match as M
from ..engine.patterns import load_patterns
from ..engine.structural import MatchEvent
from ..storage import connect
from .explain import build_evidence

_RESIDUAL_TOL = 1e-6


def build_explanations(cfg: Config, target_date: dt.date,
                       pattern_id: str | None = None) -> list[dict[str, Any]]:
    con = connect(cfg.duckdb_path)
    try:
        patterns = {p.id: p for p in load_patterns(cfg.patterns_dir)}
        sector_of, sector_etf = M._sector_maps(cfg, con)
        events = _load_events(con, target_date)
        by_ticker: dict[str, list[MatchEvent]] = {}
        for e in events:
            by_ticker.setdefault(e.ticker, []).append(e)

        abn_map = _abnormal_ret_map(con, target_date)
        firings = _best_firing_per_ticker(con, target_date, pattern_id)
        scoring_cfg = cfg.match_cfg and cfg.engine.get("scoring", {}) or {}

        evidences: list[dict[str, Any]] = []
        rows: list[tuple] = []
        for fr in firings:
            pat = patterns.get(fr["pattern_id"])
            if not pat:
                continue
            bindings = M._bind(pat, fr["ticker"], by_ticker, sector_of, sector_etf)
            catalysts = [e for e in by_ticker.get(fr["ticker"], []) if e.family in ("news", "filing")]
            p_ev = bindings.get("P")
            move_pct = p_ev.payload.get("ret_1m") if p_ev else None
            insufficient = bool(p_ev and p_ev.payload.get("grade_method") == "insufficient_history")
            feeds_ok = not insufficient and p_ev is not None
            etf = sector_etf.get(sector_of.get(fr["ticker"]))
            lead_lag = _lead_lag_strength(con, fr["ticker"], target_date)

            ev = build_evidence(
                ticker=fr["ticker"], pattern_id=pat.id, pattern_ver=pat.version,
                pattern_desc=pat.description, completeness=fr["completeness"], bindings=bindings,
                move_pct=move_pct, abnormal_move_pct=abn_map.get(fr["ticker"]),
                regime_tags=fr["regime_tags"], sector_etf=etf, lead_lag_strength=lead_lag,
                insufficient_history=insufficient, feeds_ok=feeds_ok, cfg_scoring=scoring_cfg,
                window_start=fr["window_start"], window_end=fr["window_end"], catalysts=catalysts,
            )
            _assert_residual(ev)
            evidences.append(ev)
            rows.append(_to_row(fr, ev))

        _persist(con, target_date, rows)
        return evidences
    finally:
        con.close()


def _assert_residual(ev: dict) -> None:
    total = sum(d["weight"] for d in ev["drivers"]) + ev["unexplained_residual"]
    if abs(total - 1.0) > _RESIDUAL_TOL:
        raise ValueError(f"attribution+residual != 1.0 for {ev['ticker']}: {total}")
    if ev["unexplained_residual"] <= 0:
        raise ValueError(f"residual must be > 0 (never rounded to 100%): {ev['ticker']}")


def _load_events(con, target_date) -> list[MatchEvent]:
    rows = con.execute(
        "SELECT g.event_id, g.event_time, g.ticker, n.family, g.event_type, g.abnormality, "
        "g.payload, n.payload FROM graded_events g JOIN normalized_events n USING(event_id) "
        "WHERE CAST(g.event_time AS DATE) = ? ORDER BY g.event_time, g.event_id", [target_date]
    ).fetchall()
    out = []
    for eid, et, tk, fam, etype, abn, gp, npl in rows:
        payload = {}
        if npl:
            payload.update(json.loads(npl))
        if gp:
            payload.update(json.loads(gp))  # graded features take precedence
        out.append(MatchEvent(eid, et, tk, fam, etype, abn if abn is not None else 0.0, payload))
    return out


def _abnormal_ret_map(con, target_date) -> dict[str, float]:
    rows = con.execute(
        "SELECT ticker, abnormal_ret FROM expected_behavior_1m "
        "WHERE CAST(ts AS DATE) = ? AND abnormal_ret IS NOT NULL", [target_date]
    ).fetchall()
    return {t: a for t, a in rows}


def _best_firing_per_ticker(con, target_date, pattern_id: str | None = None) -> list[dict]:
    where = "CAST(window_start AS DATE) = ?"
    params: list = [target_date]
    if pattern_id:
        where += " AND pattern_id = ?"
        params.append(pattern_id)
    rows = con.execute(
        "SELECT firing_id, ticker, pattern_id, pattern_ver, completeness, regime_tags, "
        f"window_start, window_end FROM pattern_firings WHERE {where} "
        "QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY completeness DESC, pattern_id) = 1",
        params,
    ).fetchall()
    cols = ["firing_id", "ticker", "pattern_id", "pattern_ver", "completeness", "regime_tags",
            "window_start", "window_end"]
    return [dict(zip(cols, r)) for r in rows]


def _lead_lag_strength(con, ticker, target_date) -> float:
    """Fraction of this name's edges that are causally-gated `precedes` (0 until Phase 6)."""
    row = con.execute(
        "SELECT count(*) FILTER (WHERE edge_type='precedes'), count(*) FROM event_edges "
        "WHERE ticker = ? AND CAST(created_at AS DATE) >= ?", [ticker, target_date]
    ).fetchone()
    total = row[1] or 0
    return (row[0] / total) if total else 0.0


def _to_row(fr: dict, ev: dict) -> tuple:
    eid = "expl_" + fr["firing_id"].split("_", 1)[-1]
    return (
        eid, fr["ticker"], fr["window_start"], fr["window_end"],
        ev["abnormal_move_pct"], json.dumps(ev["drivers"], default=str),
        ev["unexplained_residual"], ev["invalidation"], ev["confidence"]["tier"],
        json.dumps(ev, default=str),
    )


def _persist(con, target_date, rows: list[tuple]) -> None:
    con.execute("DELETE FROM move_explanations WHERE CAST(window_start AS DATE) = ?", [target_date])
    if rows:
        con.executemany(
            "INSERT INTO move_explanations (explanation_id, ticker, window_start, window_end, "
            "abnormal_move_pct, driver_attribution, unexplained_residual, invalidation, "
            "confidence_tier, evidence_object) VALUES (?,?,?,?,?,?,?,?,?,?)", rows,
        )
