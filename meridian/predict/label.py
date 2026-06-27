"""Forward-return labeling (ROADMAP §12.1). For each firing, label directional forward
returns at multiple horizons + MFE/MAE, conditioned on regime. No-lookahead: only prices
strictly AFTER the firing date are used. Writes historical_pattern_outcomes.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..config import Config
from ..state.prices import PriceWindow, fetch_yf_window
from ..storage import connect


@dataclass(frozen=True)
class Outcome:
    firing_id: str
    pattern_id: str
    regime_label: str
    horizon: str
    fwd_return: float   # directional (signed by the move's direction)
    mfe: float          # max favorable excursion (directional)
    mae: float          # max adverse excursion (directional)


def label_date_range(cfg: Config, start: dt.date, end: dt.date,
                     price_window: PriceWindow | None = None) -> list[Outcome]:
    con = connect(cfg.duckdb_path)
    try:
        firings = con.execute(
            "SELECT f.firing_id, f.ticker, f.pattern_id, CAST(f.window_start AS DATE), "
            "f.regime_tags FROM pattern_firings f "
            "WHERE CAST(f.window_start AS DATE) BETWEEN ? AND ? ORDER BY 4, 1", [start, end]
        ).fetchall()
        regimes = {r[0]: (r[1] or "") for r in
                   con.execute("SELECT trade_date, regime_label FROM regimes_daily").fetchall()}
    finally:
        con.close()
    if not firings:
        return []

    horizons = [int(h) for h in cfg.predict.get("horizons_days", [1, 3, 5])]
    tickers = sorted({f[1] for f in firings})
    min_d = min(f[3] for f in firings)
    max_d = max(f[3] for f in firings)
    if price_window is None:
        price_window = fetch_yf_window(tickers, min_d - dt.timedelta(days=10),
                                       max_d + dt.timedelta(days=max(horizons) * 2 + 10))

    closes = {t: _close_series(price_window.get(t, [])) for t in tickers}
    outcomes: list[Outcome] = []
    for firing_id, ticker, pattern_id, fdate, _tags in firings:
        series = closes.get(ticker)
        if not series:
            continue
        regime = regimes.get(fdate, "")
        outcomes.extend(_label_one(firing_id, pattern_id, regime, series, fdate, horizons))
    _write(cfg, start, end, outcomes)
    return outcomes


def _close_series(bars) -> list[tuple[dt.date, float]]:
    return [(b["date"], b["close"]) for b in sorted(bars, key=lambda b: b["date"])
            if b.get("close") is not None]


def _label_one(firing_id, pattern_id, regime, series, fdate, horizons) -> list[Outcome]:
    idx = next((i for i, (d, _) in enumerate(series) if d == fdate), None)
    if idx is None or idx == 0 or idx + 1 >= len(series):
        return []
    entry = series[idx][1]
    direction = 1.0 if entry >= series[idx - 1][1] else -1.0  # move direction at entry
    out: list[Outcome] = []
    for h in horizons:
        j = idx + h
        if j >= len(series):
            continue
        path = [series[k][1] for k in range(idx + 1, j + 1)]
        fwd = direction * (series[j][1] / entry - 1.0)
        excursions = [direction * (px / entry - 1.0) for px in path]
        out.append(Outcome(firing_id, pattern_id, regime, f"+{h}d", fwd,
                           max(excursions), min(excursions)))
    return out


def _write(cfg: Config, start, end, outcomes: list[Outcome]) -> None:
    con = connect(cfg.duckdb_path)
    try:
        con.execute(
            "DELETE FROM historical_pattern_outcomes WHERE firing_id IN "
            "(SELECT firing_id FROM pattern_firings WHERE CAST(window_start AS DATE) BETWEEN ? AND ?)",
            [start, end])
        if outcomes:
            con.executemany(
                "INSERT INTO historical_pattern_outcomes (firing_id, pattern_id, regime_label, "
                "horizon, fwd_return, mfe, mae) VALUES (?,?,?,?,?,?,?)",
                [(o.firing_id, o.pattern_id, o.regime_label, o.horizon, o.fwd_return, o.mfe, o.mae)
                 for o in outcomes])
    finally:
        con.close()
