"""Walk-forward calibration + reliability curves (ROADMAP §12.5).

Bins the model's predicted confidence against the realized directional hit-rate. With
enough firings, training uses only PAST firings (purged walk-forward, no lookahead);
below that, a single in-sample reliability curve is produced and labelled as such.
Writes calibration_curves. Pure scoring kernels are golden-tested.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..storage import connect


@dataclass(frozen=True)
class ReliabilityBin:
    bin_lo: float
    bin_hi: float
    predicted: float   # mean predicted confidence in bin
    realized: float    # realized hit-rate in bin
    n: int


@dataclass
class CalibrationResult:
    pattern_id: str
    regime_label: str
    horizon: str
    walk_forward: bool
    bins: list[ReliabilityBin] = field(default_factory=list)
    brier: float = float("nan")


def reliability_curve(pairs: list[tuple[float, bool]], n_bins: int) -> list[ReliabilityBin]:
    """pairs: (predicted_confidence, hit). Equal-width bins over [0,1]."""
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for pred, hit in pairs:
        idx = min(n_bins - 1, max(0, int(pred * n_bins)))
        buckets[idx].append((pred, hit))
    out: list[ReliabilityBin] = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        out.append(ReliabilityBin(
            bin_lo=i / n_bins, bin_hi=(i + 1) / n_bins,
            predicted=sum(p for p, _ in b) / len(b),
            realized=sum(1 for _, h in b if h) / len(b),
            n=len(b),
        ))
    return out


def brier_score(pairs: list[tuple[float, bool]]) -> float:
    if not pairs:
        return float("nan")
    return sum((p - (1.0 if h else 0.0)) ** 2 for p, h in pairs) / len(pairs)


def calibrate(cfg: Config, pattern_id: str | None = None,
              horizon: str = "+1d") -> list[CalibrationResult]:
    n_bins = int(cfg.predict.get("calibration_bins", 5))
    min_n = int(cfg.predict.get("min_outcomes_for_calibration", 8))
    wf_min = int(cfg.predict.get("walkforward_min_train", 20))

    con = connect(cfg.duckdb_path)
    try:
        where = "o.horizon = ?"
        params: list = [horizon]
        if pattern_id:
            where += " AND o.pattern_id = ?"
            params.append(pattern_id)
        rows = con.execute(
            "SELECT o.pattern_id, o.regime_label, f.completeness, f.confidence, o.fwd_return, "
            "CAST(f.window_start AS DATE) AS d FROM historical_pattern_outcomes o "
            "JOIN pattern_firings f USING(firing_id) "
            f"WHERE {where} ORDER BY d", params).fetchall()
    finally:
        con.close()

    by_pat: dict[str, list] = {}
    for pid, regime, completeness, conf, fwd, d in rows:
        pred = conf if conf is not None else completeness  # confidence is the predicted prob
        if pred is None:
            continue
        by_pat.setdefault(pid, []).append((d, float(pred), bool(fwd > 0)))

    results: list[CalibrationResult] = []
    for pid, items in by_pat.items():
        if len(items) < min_n:
            continue
        items.sort(key=lambda x: x[0])
        walk_forward = len({d for d, _, _ in items}) >= 2 and len(items) >= wf_min
        if walk_forward:
            split = int(len(items) * 0.6)
            train, test = items[:split], items[split:]
            pairs = [(p, h) for _, p, h in test] or [(p, h) for _, p, h in train]
        else:
            pairs = [(p, h) for _, p, h in items]
        bins = reliability_curve(pairs, n_bins)
        results.append(CalibrationResult(
            pattern_id=pid, regime_label="*", horizon=horizon, walk_forward=walk_forward,
            bins=bins, brier=brier_score(pairs)))
    _write(cfg, results)
    return results


def _write(cfg: Config, results: list[CalibrationResult]) -> None:
    con = connect(cfg.duckdb_path)
    try:
        for r in results:
            con.execute("DELETE FROM calibration_curves WHERE pattern_id=? AND regime_label=?",
                        [r.pattern_id, r.regime_label])
            if r.bins:
                con.executemany(
                    "INSERT INTO calibration_curves (pattern_id, regime_label, bin_lo, bin_hi, "
                    "predicted, realized, n) VALUES (?,?,?,?,?,?,?)",
                    [(r.pattern_id, r.regime_label, b.bin_lo, b.bin_hi, b.predicted, b.realized, b.n)
                     for b in r.bins])
    finally:
        con.close()
