"""Weekly self-improvement (Step D3).

Over the full accrued firing history: refresh historical_pattern_outcomes (label),
recompute walk-forward calibration, and report which calibration gates opened. Gated
return-based attribution lights up automatically as its data gate passes — no code change;
until then residual_basis stays "structural" (fail-closed). Prints what changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from .calibrate import calibrate
from .label import label_date_range
from ..storage import connect


@dataclass
class RelearnReport:
    start: str | None = None
    end: str | None = None
    outcomes_before: int = 0
    outcomes_after: int = 0
    calibrated_patterns: list[str] = field(default_factory=list)
    gates_opened: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def relearn(cfg: Config) -> RelearnReport:
    con = connect(cfg.duckdb_path)
    rng = con.execute(
        "SELECT CAST(min(window_start) AS DATE), CAST(max(window_start) AS DATE) FROM pattern_firings"
    ).fetchone()
    rep = RelearnReport()
    if not rng or rng[0] is None:
        con.close()
        rep.notes.append("No firings yet — nothing to relearn.")
        return rep
    rep.start, rep.end = str(rng[0]), str(rng[1])
    rep.outcomes_before = con.execute("SELECT count(*) FROM historical_pattern_outcomes").fetchone()[0]
    before_cal = {r[0] for r in con.execute(
        "SELECT DISTINCT pattern_id FROM calibration_curves").fetchall()}
    con.close()

    # 1) refresh outcomes over the full history (forward labeling)
    label_date_range(cfg, rng[0], rng[1])
    # 2) recompute walk-forward calibration for every configured horizon
    for h in cfg.predict.get("horizons_days", [1, 3, 5]):
        for r in calibrate(cfg, horizon=f"+{int(h)}d"):
            if r.pattern_id not in rep.calibrated_patterns:
                rep.calibrated_patterns.append(r.pattern_id)

    con = connect(cfg.duckdb_path)
    rep.outcomes_after = con.execute("SELECT count(*) FROM historical_pattern_outcomes").fetchone()[0]
    after_cal = {r[0] for r in con.execute(
        "SELECT DISTINCT pattern_id FROM calibration_curves").fetchall()}
    con.close()
    rep.gates_opened = sorted(after_cal - before_cal)

    rep.notes.append("Return-based attribution is fail-closed: residual_basis stays "
                     "'structural' until a pattern's data gate passes (sector_sympathy uses "
                     "its sector-beta return basis today; base-rate/temporal/microstructure "
                     "gates open automatically as their data accrues).")
    return rep
