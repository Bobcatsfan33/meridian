"""Honest paper backtest (ROADMAP §12.6). Opens a paper position off each firing,
resolves it against the real forward price at the exit horizon, and reports win-rate +
mean return WITH the mean unexplained residual attached — never a clean story.
Writes paper_trades.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..config import Config
from ..storage import connect


@dataclass
class BacktestResult:
    pattern_id: str
    horizon: str
    n_trades: int
    win_rate: float
    mean_return: float
    median_return: float
    mean_mfe: float
    mean_mae: float
    mean_residual: float          # honesty: attribution never explained 100%
    regime_breakdown: dict[str, tuple[int, float]] = field(default_factory=dict)


def backtest(cfg: Config, pattern_id: str, horizon: str | None = None) -> BacktestResult:
    horizon = horizon or f"+{int(cfg.predict.get('backtest_exit_horizon_days', 3))}d"
    con = connect(cfg.duckdb_path)
    try:
        rows = con.execute(
            "SELECT o.firing_id, o.regime_label, o.fwd_return, o.mfe, o.mae, f.ticker "
            "FROM historical_pattern_outcomes o JOIN pattern_firings f USING(firing_id) "
            "WHERE o.pattern_id=? AND o.horizon=?", [pattern_id, horizon]).fetchall()
        resid_rows = con.execute(
            "SELECT m.ticker, m.unexplained_residual FROM move_explanations m").fetchall()
    finally:
        con.close()
    resid_by_ticker: dict[str, list[float]] = {}
    for tk, r in resid_rows:
        if r is not None:
            resid_by_ticker.setdefault(tk, []).append(r)

    if not rows:
        return BacktestResult(pattern_id, horizon, 0, float("nan"), float("nan"),
                              float("nan"), float("nan"), float("nan"), float("nan"))

    rets = [r[2] for r in rows]
    mfes = [r[3] for r in rows]
    maes = [r[4] for r in rows]
    residuals = [statistics.fmean(resid_by_ticker[r[5]]) for r in rows if r[5] in resid_by_ticker]

    by_regime: dict[str, list[float]] = {}
    for _fid, regime, fwd, *_ in rows:
        by_regime.setdefault(regime or "?", []).append(fwd)
    regime_breakdown = {k: (len(v), sum(1 for x in v if x > 0) / len(v)) for k, v in by_regime.items()}

    res = BacktestResult(
        pattern_id=pattern_id, horizon=horizon, n_trades=len(rows),
        win_rate=sum(1 for r in rets if r > 0) / len(rets),
        mean_return=statistics.fmean(rets), median_return=statistics.median(rets),
        mean_mfe=statistics.fmean(mfes), mean_mae=statistics.fmean(maes),
        mean_residual=statistics.fmean(residuals) if residuals else float("nan"),
        regime_breakdown=regime_breakdown,
    )
    _write_paper_trades(cfg, pattern_id, horizon, rows)
    return res


def _write_paper_trades(cfg: Config, pattern_id, horizon, rows) -> None:
    con = connect(cfg.duckdb_path)
    try:
        ids = [r[0] for r in rows]
        if ids:
            con.execute("DELETE FROM paper_trades WHERE firing_id IN (%s)" % ",".join("?" * len(ids)), ids)
        con.executemany(
            "INSERT INTO paper_trades (trade_id, firing_id, ticker, ret, win) VALUES (?,?,?,?,?)",
            [(f"pt_{r[0]}_{horizon}", r[0], r[5], r[2], r[2] > 0) for r in rows])
    finally:
        con.close()
