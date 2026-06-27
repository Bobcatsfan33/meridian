"""L2 match orchestration (Phase 3): graded events -> pattern firings + audited edges.

For each target stock with a daily move, bind each pattern's roles to that name's
graded events (the sector role binds to the name's sector ETF), score completeness
(graded, never boolean), and persist firings + event_edges. Every edge carries a
rule_id; observed `precedes` relations are gated by causal.py (downgraded until Phase 6).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field

from . import causal, structural
from .patterns import Pattern, load_patterns
from ..config import Config
from ..storage import connect


@dataclass
class MatchSummary:
    target_date: dt.date
    n_targets: int = 0
    n_firings: int = 0
    n_edges: int = 0
    per_pattern: dict[str, int] = field(default_factory=dict)
    mean_completeness: float = float("nan")
    top: list[tuple[str, str, float]] = field(default_factory=list)  # (ticker, rule_id, completeness)


def run_match(cfg: Config, target_date: dt.date, pattern_ids: list[str] | None = None) -> MatchSummary:
    con = connect(cfg.duckdb_path)
    try:
        patterns = load_patterns(cfg.patterns_dir)
        if pattern_ids:
            patterns = [p for p in patterns if p.id in set(pattern_ids)]
        windows = _windows(cfg)
        alpha = cfg.causal_test_alpha
        min_comp = float(cfg.match_cfg.get("min_completeness", 0.5))

        events = _day_events(con, target_date)
        if not events:
            return MatchSummary(target_date=target_date)

        by_ticker: dict[str, list[structural.MatchEvent]] = {}
        for e in events:
            by_ticker.setdefault(e.ticker, []).append(e)

        sector_of, sector_etf = _sector_maps(cfg, con)
        regime_tags = _regime_tags(con, target_date)
        day_start = dt.datetime.combine(target_date, dt.time(0, 0))
        day_end = dt.datetime.combine(target_date, dt.time(23, 59, 59))

        firings: list[tuple] = []
        edge_rows: list[tuple] = []
        targets = [t for t, evs in by_ticker.items()
                   if t and any(ev.family == "price_volume" for ev in evs)]
        comps: list[float] = []

        for ticker in sorted(targets):
            present_families = {ev.family for ev in by_ticker[ticker]}
            for pat in patterns:
                bindings = _bind(pat, ticker, by_ticker, sector_of, sector_etf)
                result = structural.evaluate(pat, bindings, present_families, windows)
                if result.completeness < min_comp:
                    continue
                comps.append(result.completeness)
                fid = _id("fire", pat.rule_id, ticker, target_date.isoformat())
                firings.append((fid, ticker, pat.id, pat.version, day_start, day_end,
                                result.completeness, None, list(regime_tags)))
                for edge in result.edges:
                    v = causal.gate_precedence(edge.observed_relation, alpha)
                    eid = _id("edge", pat.rule_id, edge.src.event_id, edge.dst.event_id, edge.observed_relation)
                    edge_rows.append((eid, edge.src.event_id, edge.dst.event_id, ticker,
                                      v.edge_type, edge.lag_seconds, v.test_stat, v.test_pvalue,
                                      edge.score, pat.rule_id))

        ran_rule_ids = {p.rule_id for p in patterns}
        _persist(con, target_date, [e.event_id for e in events], firings, edge_rows,
                 {p.id for p in patterns}, ran_rule_ids)

        per_pattern: dict[str, int] = {}
        for f in firings:
            per_pattern[f[2]] = per_pattern.get(f[2], 0) + 1
        top = sorted(((f[1], f"{f[2]}@{f[3]}", f[6]) for f in firings),
                     key=lambda x: -x[2])[:10]
        return MatchSummary(
            target_date=target_date, n_targets=len(targets), n_firings=len(firings),
            n_edges=len(edge_rows), per_pattern=dict(sorted(per_pattern.items())),
            mean_completeness=(sum(comps) / len(comps)) if comps else float("nan"), top=top,
        )
    finally:
        con.close()


def _windows(cfg: Config) -> structural.MatchWindows:
    m = cfg.match_cfg
    return structural.MatchWindows(
        concurrent_window_s=float(m.get("concurrent_window_seconds", 86400)),
        precedes_min_lag_s=float(m.get("precedes_min_lag_seconds", 0)),
        precedes_max_window_s=float(m.get("precedes_max_window_seconds", 86400)),
    )


def _day_events(con, target_date) -> list[structural.MatchEvent]:
    rows = con.execute(
        "SELECT g.event_id, g.event_time, g.ticker, n.family, g.event_type, g.abnormality, g.payload "
        "FROM graded_events g JOIN normalized_events n USING(event_id) "
        "WHERE CAST(g.event_time AS DATE) = ? ORDER BY g.event_time, g.event_id",
        [target_date],
    ).fetchall()
    out = []
    for eid, et, tk, fam, etype, abn, payload in rows:
        out.append(structural.MatchEvent(
            event_id=eid, event_time=et, ticker=tk, family=fam, event_type=etype,
            abnormality=abn if abn is not None else 0.0,
            payload=json.loads(payload) if payload else {},
        ))
    return out


def _bind(pat: Pattern, ticker, by_ticker, sector_of, sector_etf):
    bindings: dict[str, structural.MatchEvent | None] = {}
    for role in pat.roles:
        if role.bind == "sector_etf":
            etf = sector_etf.get(sector_of.get(ticker))
            cands = by_ticker.get(etf, []) if etf else []
            cands = [e for e in cands if e.family == role.family]
        else:
            cands = [e for e in by_ticker.get(ticker, []) if e.family == role.family]
        if role.event_type:
            cands = [e for e in cands if e.event_type == role.event_type]
        bindings[role.name] = _pick(cands, role.pick)
    return bindings


def _pick(cands, how):
    if not cands:
        return None
    if how == "first":
        return min(cands, key=lambda e: e.event_time)
    return max(cands, key=lambda e: e.abnormality)


def _sector_maps(cfg: Config, con):
    sector_of = {r[0]: r[1] for r in con.execute("SELECT symbol, sector FROM universe").fetchall()}
    sector_etf: dict[str, str] = {}
    if cfg.index_etf_file.exists():
        with cfg.index_etf_file.open() as fh:
            for r in csv.DictReader(fh):
                if (r.get("role") or "").strip() == "sector":
                    sector_etf[(r.get("description") or "").strip()] = r["symbol"]
    return sector_of, sector_etf


def _regime_tags(con, target_date) -> tuple[str, ...]:
    row = con.execute("SELECT regime_tags FROM regimes_daily WHERE trade_date=?",
                      [target_date]).fetchone()
    return tuple(row[0]) if row and row[0] else ()


def _persist(con, target_date, day_event_ids, firings, edge_rows, ran_pattern_ids, ran_rule_ids) -> None:
    # Scope deletes to the patterns actually run, so `match --patterns X` is additive
    # for other patterns rather than wiping the day's firings.
    pids = list(ran_pattern_ids)
    con.execute(
        "DELETE FROM pattern_firings WHERE CAST(window_start AS DATE) = ? AND pattern_id IN (%s)"
        % ",".join("?" * len(pids)),
        [target_date, *pids],
    )
    rids = list(ran_rule_ids)
    if day_event_ids and rids:
        con.execute(
            "DELETE FROM event_edges WHERE rule_id IN (%s) AND src_event_id IN (%s)"
            % (",".join("?" * len(rids)), ",".join("?" * len(day_event_ids))),
            [*rids, *day_event_ids],
        )
    if firings:
        con.executemany(
            "INSERT INTO pattern_firings (firing_id, ticker, pattern_id, pattern_ver, "
            "window_start, window_end, completeness, confidence, regime_tags) "
            "VALUES (?,?,?,?,?,?,?,?,?)", firings,
        )
    if edge_rows:
        con.executemany(
            "INSERT INTO event_edges (edge_id, src_event_id, dst_event_id, ticker, edge_type, "
            "lag_seconds, test_stat, test_pvalue, confidence, rule_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            edge_rows,
        )


def _id(*parts: str) -> str:
    return "%s_%s" % (parts[0], hashlib.sha1("|".join(parts[1:]).encode()).hexdigest()[:18])
