"""Build + persist move_explanations for a date (one card per ticker, best firing).

Reloads the day's graded events (payloads merged from normalized+graded for readable
timelines), rebinds each chosen firing's pattern roles, builds the evidence object, and
writes move_explanations. Enforces attribution + residual == 1.0 before persisting.

Also builds the machine-readable daily digest (feed/meridian-latest.json): bucketed
scanner rows with honest provenance, corporate-action and sympathy-confidence gates,
and counts that reflect the PUBLISHED arrays.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

from ..config import Config
from ..engine import match as M
from ..engine.patterns import load_patterns
from ..engine.structural import MatchEvent
from ..ingest.clock import market_close_utc
from ..state import baseline as bl
from ..storage import connect
from . import phrases
from .explain import build_evidence

_RESIDUAL_TOL = 1e-6

# --- digest gates (feed/meridian-latest.json) ---------------------------------------
_DIGEST_TOP_N = 40            # rows published per bucket
_DIGEST_MAX_MOVE = 0.25       # |move| above this with NO catalyst -> suspect corporate action
_SYMPATHY_MAX_RESIDUAL = 0.6  # return-basis residual at/above this -> low confidence


def card_for_ticker(cfg: Config, ticker: str, target_date: dt.date) -> dict[str, Any]:
    """Resolve one name+date to a structured evidence object (the same shape the scanner
    cards use). Never empty, never fabricated:
      (a) a stored move_explanations row -> return it verbatim (no re-scoring);
      (b) a universe name with no firing  -> a graceful 'no supported explanation' read;
      (c) anything else                   -> an 'ad-hoc / not tracked' graceful read.
    """
    ticker = (ticker or "").strip().upper()
    con = connect(cfg.duckdb_path)
    try:
        row = con.execute(
            "SELECT evidence_object FROM move_explanations WHERE ticker=? AND "
            "CAST(window_start AS DATE)=?", [ticker, target_date]).fetchone()
        if row and row[0]:
            return json.loads(row[0])  # (a) same object the scanner row uses
        in_universe = con.execute("SELECT 1 FROM universe WHERE symbol=?", [ticker]).fetchone() is not None
        return _graceful_card(con, ticker, target_date, in_universe=in_universe)
    finally:
        con.close()


def _graceful_card(con, ticker: str, target_date: dt.date, in_universe: bool,
                   ad_hoc: bool = False) -> dict[str, Any]:
    """An honest 'no supported explanation' evidence object built from existing state —
    NEVER invents a pattern. tier=Unknown, residual ~100%. Renders like any other card."""
    close = market_close_utc(target_date).replace(tzinfo=None)
    move = _scalar(con, "SELECT ret_1m FROM ticker_state_1m WHERE ticker=? AND ts=?", [ticker, close])
    abnormal = _scalar(con, "SELECT abnormal_ret FROM expected_behavior_1m WHERE ticker=? AND ts=?",
                       [ticker, close])
    reg = con.execute("SELECT regime_tags FROM regimes_daily WHERE trade_date=?", [target_date]).fetchone()
    regime_tags = list(reg[0]) if reg and reg[0] else []

    if not in_universe and not ad_hoc:
        desc = "Not part of the tracked universe."
        readout = "Not tracked — no data for this name on this date."
    else:
        desc = "No supported explanation"
        readout = "No supported explanation — moved in line with expectations."
    return {
        "ticker": ticker,
        "window_start": str(dt.datetime.combine(target_date, dt.time())),
        "window_end": str(dt.datetime.combine(target_date, dt.time(23, 59, 59))),
        "pattern": {"id": "none", "version": "0", "description": desc, "completeness": 0.0},
        "move_pct": move,
        "abnormal_move_pct": abnormal,
        "confidence": {"value": 0.0, "tier": "Unknown"},
        "tier_phrase": phrases.tier_phrase("Unknown"),
        "tier_verb": phrases.tier_verb("Unknown"),
        "readout": readout,
        "drivers": [],                       # nothing attributable -> residual is ~100%
        "unexplained_residual": 1.0,
        "residual_basis": "structural",
        "constraints_applied": ["No firing pattern for this name on this date."],
        "regime_tags": regime_tags,
        "move_class": "none",
        "data_source": "ad_hoc" if ad_hoc else ("universe" if in_universe else "n/a"),
        "proxy_data": False,
        "ad_hoc": ad_hoc,
        "timeline": [],
        "invalidation": "A read would emerge if an abnormal driver (news, flow, filing, or "
                        "dealer positioning) appears, or the move diverges from its expected behavior.",
        "not_investment_advice": True,
    }


def _scalar(con, sql: str, params: list):
    row = con.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else None


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
            explained_fraction, residual_basis = _return_residual(
                con, fr["pattern_id"], fr["ticker"], etf, target_date,
                abn_map.get(fr["ticker"]), cfg)
            data_source = _options_data_source(bindings)

            ev = build_evidence(
                ticker=fr["ticker"], pattern_id=pat.id, pattern_ver=pat.version,
                pattern_desc=pat.description, completeness=fr["completeness"], bindings=bindings,
                move_pct=move_pct, abnormal_move_pct=abn_map.get(fr["ticker"]),
                regime_tags=fr["regime_tags"], sector_etf=etf, lead_lag_strength=lead_lag,
                insufficient_history=insufficient, feeds_ok=feeds_ok, cfg_scoring=scoring_cfg,
                window_start=fr["window_start"], window_end=fr["window_end"], catalysts=catalysts,
                explained_fraction=explained_fraction, residual_basis=residual_basis,
                data_source=data_source,
            )
            _assert_residual(ev, float(scoring_cfg.get("min_residual", 0.05)))
            evidences.append(ev)
            rows.append(_to_row(fr, ev))

        # Only persist the FULL set. A pattern-filtered build (e.g. `card --pattern X`) is a
        # render path and must not clobber the day's stored move_explanations.
        if pattern_id is None:
            _persist(con, target_date, rows)
        return evidences
    finally:
        con.close()


def _assert_residual(ev: dict, min_resid: float) -> None:
    total = sum(d["weight"] for d in ev["drivers"]) + ev["unexplained_residual"]
    if abs(total - 1.0) > _RESIDUAL_TOL:
        raise ValueError(f"attribution+residual != 1.0 for {ev['ticker']}: {total}")
    if ev["unexplained_residual"] < min_resid - _RESIDUAL_TOL:
        raise ValueError(f"residual below floor {min_resid} for {ev['ticker']}: "
                         f"{ev['unexplained_residual']}")
    if ev.get("residual_basis") not in ("return", "structural"):
        raise ValueError(f"missing/invalid residual_basis for {ev['ticker']}: {ev.get('residual_basis')}")


def _return_residual(con, pattern_id, ticker, etf, target_date, abnormal_move, cfg):
    """Return-based unexplained share of the abnormal move (ROADMAP §11).

    sector_sympathy: decompose the name's abnormal move into the part its sector
    explains. attributed = beta_to_sector * sector_move; residual_return = abnormal_move
    - attributed; residual_fraction = clip(|residual_return| / |abnormal_move|, floor, 1).
    Returns (explained_fraction, "return") or (None, "structural") when no return basis
    is defensible (other patterns, or missing inputs) — the caller then uses the
    structural completeness residual.
    """
    min_resid = float(cfg.engine.get("scoring", {}).get("min_residual", 0.05))
    if pattern_id != "sector_sympathy" or not etf or abnormal_move is None or abnormal_move == 0:
        return None, "structural"
    close_ts = market_close_utc(target_date).replace(tzinfo=None)
    sector_move = _ret_on(con, etf, close_ts)
    if sector_move is None:
        return None, "structural"
    win = int(cfg.feat("beta_window_days", 60))
    beta = _beta_to_sector(con, ticker, etf, close_ts, win)
    if beta is None:
        return None, "structural"
    attributed = beta * sector_move
    residual_return = abnormal_move - attributed
    residual_fraction = min(1.0, max(min_resid, abs(residual_return) / abs(abnormal_move)))
    return 1.0 - residual_fraction, "return"


def _options_data_source(bindings: dict) -> str:
    """Provenance of the options legs: the actual provider (massive|yfinance|fixture).
    Fixture (synthetic) is proxy data. Patterns bound WITHOUT any options leg never saw
    options data at all -> 'price_volume_proxy' (price+volume inference only) — NEVER
    'live', which would misrepresent a proxy read as options-backed."""
    provider = None
    has_options_leg = False
    for ev in bindings.values():
        if ev is not None and ev.family == "dealer_pos":
            has_options_leg = True
            ds = (ev.payload or {}).get("data_source")
            if ds == "fixture":
                return "fixture"        # any synthetic leg -> proxy
            provider = provider or ds
    if not has_options_leg:
        return "price_volume_proxy"     # no options data behind this read
    return provider or "live"


def _ret_on(con, ticker, close_ts):
    row = con.execute("SELECT ret_1m FROM ticker_state_1m WHERE ticker=? AND ts=?",
                      [ticker, close_ts]).fetchone()
    return row[0] if row and row[0] is not None else None


def _beta_to_sector(con, ticker, etf, close_ts, win):
    """OLS beta of the name's returns on its sector ETF's returns, strictly BEFORE the
    target close (no-lookahead). None if too little paired history."""
    stock = dict(con.execute(
        "SELECT ts, ret_1m FROM ticker_state_1m WHERE ticker=? AND ts<? AND ret_1m IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?", [ticker, close_ts, win]).fetchall())
    sect = dict(con.execute(
        "SELECT ts, ret_1m FROM ticker_state_1m WHERE ticker=? AND ts<? AND ret_1m IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?", [etf, close_ts, win]).fetchall())
    common = sorted(set(stock) & set(sect))
    if len(common) < 2:
        return None
    beta, _alpha = bl.beta_alpha([stock[t] for t in common], [sect[t] for t in common])
    return None if beta != beta else beta  # nan -> None


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


# --- daily machine digest (feed/meridian-latest.json) --------------------------------
def build_digest(cfg: Config, target_date: dt.date,
                 evidences: list[dict[str, Any]] | None = None, *,
                 options_layer_ran: bool | None = None,
                 top_n: int = _DIGEST_TOP_N) -> dict[str, Any]:
    """The machine-readable EOD digest consumed by downstream agents (Helm).

    Honesty gates (nothing is silently dropped — excluded rows move to labeled lists):
      - flow candidates with |move| > _DIGEST_MAX_MOVE and NO news/filing catalyst are
        moved to `suspect_corporate_action` (likely split/spinoff artifact, not flow);
      - sympathy rows whose return-basis residual >= _SYMPATHY_MAX_RESIDUAL (the sector
        explained almost none of the move) are moved to `sympathy_low_confidence`;
      - `counts.*` reflect the PUBLISHED array lengths; `counts_total_pre_truncation`
        carries the pre-truncation totals;
      - `options_layer_ran` / `options_coverage` distinguish "no gamma squeezes" from
        "the options layer never ran".
    """
    con = connect(cfg.duckdb_path)
    try:
        if evidences is None:
            stored = con.execute(
                "SELECT evidence_object FROM move_explanations WHERE CAST(window_start AS DATE)=?",
                [target_date]).fetchall()
            evidences = [json.loads(b) for (b,) in stored if b]
        universe = {r[0] for r in con.execute("SELECT symbol FROM universe").fetchall()}
        reg = con.execute("SELECT regime_label, regime_tags FROM regimes_daily WHERE trade_date=?",
                          [target_date]).fetchone()
        coverage = _options_coverage(con, target_date)
    finally:
        con.close()
    if options_layer_ran is None:
        options_layer_ran = coverage > 0   # standalone builds: infer from real options events

    def bucket(pattern_ids: set[str]) -> list[dict]:
        rows = [e for e in evidences if e["pattern"]["id"] in pattern_ids]
        rows.sort(key=lambda e: e["confidence"]["value"], reverse=True)
        return rows

    gamma = bucket({"gamma_squeeze"})
    flow = bucket({"options_led_proxy", "dark_pool_accumulation"})
    pbn = bucket({"price_before_news"})
    symp = bucket({"sector_sympathy"})

    # corporate-action sanity gate: a >25% "flow" move with no catalyst evidence is far
    # more likely a split/spinoff price artifact than dealer flow.
    flow_ok: list[dict] = []
    suspects: list[dict] = []
    for e in flow:
        mv = e.get("move_pct")
        if isinstance(mv, (int, float)) and abs(mv) > _DIGEST_MAX_MOVE and not _has_catalyst(e):
            suspects.append(e)
        else:
            flow_ok.append(e)

    # sympathy confidence gate: return-basis residual >= threshold means the sector
    # decomposition explained (almost) none of the move — not a defensible deprioritize.
    symp_ok: list[dict] = []
    symp_low: list[dict] = []
    for e in symp:
        if e.get("residual_basis") == "return" and \
                float(e.get("unexplained_residual", 1.0)) >= _SYMPATHY_MAX_RESIDUAL:
            symp_low.append(e)
        else:
            symp_ok.append(e)

    def publish(rows: list[dict]) -> list[dict]:
        return [_digest_row(e, e["ticker"] in universe) for e in rows[:top_n]]

    published = {
        "gamma_squeeze": publish(gamma),
        "flow_candidates": publish(flow_ok),
        "price_before_news": publish(pbn),
        "sympathy_beta_deprioritize": publish(symp_ok),
        "suspect_corporate_action": publish(suspects),
        "sympathy_low_confidence": publish(symp_low),
    }
    counts = {
        "flow_candidates": len(published["flow_candidates"]),
        "gamma_squeeze": len(published["gamma_squeeze"]),
        "price_before_news": len(published["price_before_news"]),
        "sympathy_beta": len(published["sympathy_beta_deprioritize"]),
        "suspect_corporate_action": len(published["suspect_corporate_action"]),
        "sympathy_low_confidence": len(published["sympathy_low_confidence"]),
    }
    totals = {
        "flow_candidates": len(flow_ok),
        "gamma_squeeze": len(gamma),
        "price_before_news": len(pbn),
        "sympathy_beta": len(symp_ok),
        "suspect_corporate_action": len(suspects),
        "sympathy_low_confidence": len(symp_low),
    }
    return {
        "source": "meridian",
        "meridian_date": target_date.isoformat(),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "regime": {
            "regime": reg[0] if reg else None,
            "tags": list(reg[1]) if reg and reg[1] else [],
            "names_explained": len(evidences),
        },
        "options_layer_ran": bool(options_layer_ran),
        "options_coverage": coverage,
        "counts": counts,
        "counts_total_pre_truncation": totals,
        "notes": (
            "EOD attribution of the prior session. Use as pre-market regime + watchlist + "
            "flow-confirmation overlay ONLY; not an intraday trigger. Lower residual = the "
            "labeled mechanism explains more of the move. Confidence is 0-1. "
            "data_source 'price_volume_proxy' = price+volume inference, NO options data. "
            "flow_candidates excludes |move|>25% rows with no catalyst (see "
            "suspect_corporate_action); sympathy_beta_deprioritize requires return-basis "
            "residual < 0.6 (see sympathy_low_confidence). counts reflect the published "
            "arrays; counts_total_pre_truncation carries pre-truncation totals. "
            "options_layer_ran=false means the options layer FAILED — gamma_squeeze being "
            "empty is then 'unknown', not 'none'. Helm must still require its own intraday "
            "trigger and obey all RISK LIMITS."
        ),
        **published,
    }


def write_digest(cfg: Config, target_date: dt.date,
                 evidences: list[dict[str, Any]] | None = None, *,
                 options_layer_ran: bool | None = None,
                 path: str | pathlib.Path | None = None) -> dict[str, Any]:
    """Build the digest and write it to feed/meridian-latest.json (or `path`)."""
    digest = build_digest(cfg, target_date, evidences, options_layer_ran=options_layer_ran)
    out = pathlib.Path(path) if path else (cfg.root / "feed" / "meridian-latest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(digest, indent=2, default=str) + "\n")
    return digest


def _digest_row(ev: dict, in_universe: bool) -> dict[str, Any]:
    """One digest row from an evidence object (same projection as the dashboard scanner)."""
    mv = ev.get("move_pct")
    return {
        "ticker": ev["ticker"],
        "move_pct": mv,
        "pattern": ev["pattern"]["id"],
        "tier": ev["confidence"]["tier"],
        "confidence": ev["confidence"]["value"],
        "residual": ev["unexplained_residual"],
        "residual_basis": ev.get("residual_basis"),
        "data_source": ev.get("data_source"),
        "proxy_data": ev.get("proxy_data", False),
        "move_class": ev.get("move_class"),
        "initiating": ev["timeline"][0]["label"] if ev.get("timeline") else None,
        "in_universe": in_universe,
        "move_pct_display": round(mv * 100, 2) if isinstance(mv, (int, float)) else None,
    }


def _has_catalyst(ev: dict) -> bool:
    """True if the evidence carries any news/filing event in its timeline."""
    return any(t.get("family") in ("news", "filing") for t in ev.get("timeline", []) or [])


def _options_coverage(con, target_date) -> int:
    """Symbols with REAL (non-fixture) options data on the date."""
    row = con.execute(
        "SELECT count(DISTINCT ticker) FROM normalized_events "
        "WHERE family='dealer_pos' AND CAST(event_time AS DATE)=? "
        "AND (data_source IS NULL OR data_source != 'fixture')", [target_date]).fetchone()
    return int(row[0]) if row and row[0] else 0
