"""Causal-link testing over event edges (ROADMAP §12.4).

For each cross-ticker edge on a date, test whether the source name's returns Granger-cause
the destination name's returns over trailing history. Edges that pass the gate
(p < engine.causal_test_alpha) are upgraded to a trusted `precedes` edge; the rest keep
their downgraded type. Every tested edge records test_stat + test_pvalue.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..config import Config
from ..engine.causal import gate_precedence, granger_pvalue
from ..storage import connect


@dataclass
class CausalSummary:
    target_date: dt.date
    n_edges: int = 0
    n_tested: int = 0
    n_precedes: int = 0


def run_causal_tests(cfg: Config, target_date: dt.date, max_lag: int = 3) -> CausalSummary:
    alpha = cfg.causal_test_alpha
    win = int(cfg.feat("baseline_window_days", 60))
    con = connect(cfg.duckdb_path)
    try:
        edges = con.execute(
            "SELECT e.edge_id, e.src_event_id, e.dst_event_id, ns.ticker, nd.ticker "
            "FROM event_edges e "
            "JOIN normalized_events ns ON ns.event_id = e.src_event_id "
            "JOIN normalized_events nd ON nd.event_id = e.dst_event_id "
            "WHERE CAST(e.created_at AS DATE) >= ?", [target_date]).fetchall()
        summary = CausalSummary(target_date=target_date, n_edges=len(edges))
        close_ts = _close_ts(target_date)
        for edge_id, _src, _dst, src_tkr, dst_tkr in edges:
            if not src_tkr or not dst_tkr or src_tkr == dst_tkr:
                continue  # same-name edge: cannot lead itself
            cause = _ret_series(con, src_tkr, close_ts, win)
            effect = _ret_series(con, dst_tkr, close_ts, win)
            f_stat, pval = granger_pvalue(cause, effect, max_lag)
            if pval != pval:
                continue
            summary.n_tested += 1
            verdict = gate_precedence("precedes", alpha, f_stat, pval)
            if verdict.edge_type == "precedes":
                summary.n_precedes += 1
            con.execute(
                "UPDATE event_edges SET edge_type=?, test_stat=?, test_pvalue=? WHERE edge_id=?",
                [verdict.edge_type, f_stat, pval, edge_id])
        return summary
    finally:
        con.close()


def _ret_series(con, ticker, close_ts, win) -> list[float]:
    # lookahead-ok: this is NOT a feature predicting the same day — it is the realized
    # return series through the run date's close, used after close to test a HISTORICAL
    # lead-lag relationship for that day's edges. Including the close (ts<=) is the
    # correct point-in-time series available when the causal test runs (EOD).
    rows = con.execute(
        "SELECT ret_1m FROM ticker_state_1m WHERE ticker=? AND ts<=? AND ret_1m IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?", [ticker, close_ts, win]).fetchall()
    return [r[0] for r in reversed(rows)]


def _close_ts(target_date: dt.date):
    from ..ingest.clock import market_close_utc
    return market_close_utc(target_date).replace(tzinfo=None)
