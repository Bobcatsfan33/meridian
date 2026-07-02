"""Pipeline jobs for the scheduler (ROADMAP §5 run modes).

`run_postclose` is the EOD batch driver: ingest → featurize → match → explanations.
Used by both `meridian schedule` (APScheduler) and directly for one-off runs.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field

from ..config import Config

_FLOW_PATTERNS = ["gamma_squeeze", "dark_pool_accumulation"]

logger = logging.getLogger("meridian.jobs")


@dataclass
class DayRunResult:
    target_date: dt.date
    normalized: int = 0
    graded: int = 0
    firings: int = 0
    options_events: int = 0
    flow_firings: int = 0
    explanations: int = 0
    labeled: int = 0
    options_layer_ran: bool = False
    options_coverage: int = 0
    steps: list[str] = field(default_factory=list)


def default_adapters(cfg: Config) -> list[str]:
    """Free baseline always; Massive only when opt-in (enabled + key)."""
    base = ["yfinance", "fred", "edgar", "news_rss", "finra"]
    mas = (cfg.raw.get("adapters", {}) or {}).get("massive", {}) or {}
    if mas.get("enabled") and os.environ.get(mas.get("api_key_env", "MASSIVE_API_KEY")):
        base.append("massive")
    return base


def run_postclose(
    cfg: Config, target_date: dt.date, adapters: list[str] | None = None
) -> DayRunResult:
    """Full EOD batch for one trading day. Idempotent per date; never fatal on a feed."""
    from ..engine.featurize_run import run_featurize
    from ..engine.match import run_match
    from ..ingest.pipeline import run_ingest
    from ..options.ingest import run_options
    from ..outputs.build import build_explanations, write_digest
    from ..predict.label import label_date_range

    res = DayRunResult(target_date=target_date)
    selected = adapters or default_adapters(cfg)

    res.normalized = run_ingest(cfg, target_date, selected=selected).total_normalized
    res.steps.append("ingest")

    _state, feat = run_featurize(cfg, target_date)
    res.graded = feat.n_graded
    res.steps.append("featurize")

    res.firings = run_match(cfg, target_date).n_firings
    res.steps.append("match")

    # options (real chain: massive when enabled+healthy, else yfinance live) -> dealer_pos
    try:
        res.options_events = run_options(cfg, target_date).n_events
        res.steps.append("options")
        run_featurize(cfg, target_date)                       # re-grade incl. dealer_pos
        res.flow_firings = run_match(cfg, target_date, pattern_ids=_FLOW_PATTERNS).n_firings
        res.steps.append("match:flow")
        res.options_layer_ran = True
    except Exception:  # options is enhancement-only; never fatal — but never silent either
        res.options_layer_ran = False
        logger.exception(
            "options layer failed for %s; continuing without dealer_pos "
            "(digest will carry options_layer_ran=False)", target_date)

    evidences = build_explanations(cfg, target_date)
    res.explanations = len(evidences)
    res.steps.append("explanations")

    # machine digest (feed/meridian-latest.json) with options status threaded through,
    # so consumers can tell "no gamma squeezes" from "options layer never ran".
    try:
        digest = write_digest(cfg, target_date, evidences,
                              options_layer_ran=res.options_layer_ran)
        res.options_coverage = int(digest.get("options_coverage", 0))
        res.steps.append("digest")
    except Exception:  # digest is an output artifact; never fatal for the batch
        logger.exception("digest build failed for %s", target_date)

    res.labeled = len(label_date_range(cfg, target_date, target_date))
    res.steps.append("label")
    return res


def backup_db(cfg: Config, retain: int = 14, stamp: str | None = None) -> str | None:
    """Copy the DuckDB file to data/backups/meridian-YYYYMMDD.duckdb, retain N newest."""
    import shutil

    src = cfg.duckdb_path
    if not src.exists():
        return None
    bdir = cfg.root / "data" / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    name = f"meridian-{stamp}.duckdb" if stamp else f"meridian-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')}.duckdb"
    dest = bdir / name
    shutil.copy2(src, dest)
    backups = sorted(bdir.glob("meridian-*.duckdb"))
    for old in backups[:-retain] if retain > 0 else []:
        old.unlink(missing_ok=True)
    return str(dest)


def default_premarket_et(cfg: Config) -> str:
    return (cfg.raw.get("run_modes", {}).get("eod_batch", {}) or {}).get("premarket_scan_et", "08:30")


def default_postclose_et(cfg: Config) -> str:
    return (cfg.raw.get("run_modes", {}).get("eod_batch", {}) or {}).get("postclose_postmortem_et", "16:30")
