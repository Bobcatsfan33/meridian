"""Pipeline jobs for the scheduler (ROADMAP §5 run modes).

`run_postclose` is the EOD batch driver: ingest → featurize → match → explanations.
Used by both `meridian schedule` (APScheduler) and directly for one-off runs.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..config import Config


@dataclass
class DayRunResult:
    target_date: dt.date
    normalized: int = 0
    graded: int = 0
    firings: int = 0
    explanations: int = 0
    steps: list[str] = field(default_factory=list)


def run_postclose(
    cfg: Config, target_date: dt.date, adapters: list[str] | None = None
) -> DayRunResult:
    """Full EOD batch for one trading day."""
    from ..engine.featurize_run import run_featurize
    from ..engine.match import run_match
    from ..ingest.pipeline import run_ingest
    from ..outputs.build import build_explanations

    res = DayRunResult(target_date=target_date)
    selected = adapters or ["yfinance", "fred", "edgar", "news_rss"]

    ingest = run_ingest(cfg, target_date, selected=selected)
    res.normalized = ingest.total_normalized
    res.steps.append("ingest")

    _state, feat = run_featurize(cfg, target_date)
    res.graded = feat.n_graded
    res.steps.append("featurize")

    match = run_match(cfg, target_date)
    res.firings = match.n_firings
    res.steps.append("match")

    evidences = build_explanations(cfg, target_date)
    res.explanations = len(evidences)
    res.steps.append("explanations")
    return res


def default_premarket_et(cfg: Config) -> str:
    return (cfg.raw.get("run_modes", {}).get("eod_batch", {}) or {}).get("premarket_scan_et", "08:30")


def default_postclose_et(cfg: Config) -> str:
    return (cfg.raw.get("run_modes", {}).get("eod_batch", {}) or {}).get("postclose_postmortem_et", "16:30")
