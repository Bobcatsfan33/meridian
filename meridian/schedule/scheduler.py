"""APScheduler wiring (ROADMAP §5, Phase 7). Both run modes:
  - pre-market scan (~08:30 ET, cron),
  - intraday polling loop (every poll_seconds, gated to market hours) emitting live cards,
  - post-close postmortem (~16:30 ET, cron).
Times are exchange-local (America/New_York). A simple alert hook fires on High-confidence cards.
"""
from __future__ import annotations

import datetime as dt

from ..config import Config
from ..ingest.clock import MARKET_OPEN_LOCAL, MARKET_CLOSE_LOCAL, MARKET_TZ
from .jobs import backup_db, default_postclose_et, default_premarket_et, run_postclose


def _today_et() -> dt.date:
    return dt.datetime.now(MARKET_TZ).date()


def _in_market_hours() -> bool:
    now = dt.datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN_LOCAL <= now.time() <= MARKET_CLOSE_LOCAL


def emit_alerts(cfg: Config, target_date: dt.date) -> int:
    """Append High-confidence cards to a local alerts log (alerting hook)."""
    import json

    from ..storage import connect

    con = connect(cfg.duckdb_path)
    try:
        rows = con.execute(
            "SELECT ticker, confidence_tier, evidence_object FROM move_explanations "
            "WHERE CAST(window_start AS DATE)=? AND confidence_tier='High'", [target_date]).fetchall()
    finally:
        con.close()
    if not rows:
        return 0
    log = cfg.root / "data" / "alerts.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        for ticker, tier, blob in rows:
            ev = json.loads(blob)
            fh.write(f"{target_date} {ticker} {tier} {ev['pattern']['id']} "
                     f"move={ev.get('move_pct')} resid={ev['unexplained_residual']}\n")
    return len(rows)


def build_scheduler(cfg: Config, mode: str = "postclose"):
    """Construct a BlockingScheduler with the requested jobs registered (not started)."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    sched = BlockingScheduler(timezone=str(MARKET_TZ),
                              job_defaults={"coalesce": True, "max_instances": 1})  # overlap-skip

    def premarket_job():
        d = _today_et()
        run_postclose(cfg, d)
        emit_alerts(cfg, d)

    def postclose_job():
        d = _today_et()
        run_postclose(cfg, d)        # ingest -> ... -> explanations -> label
        emit_alerts(cfg, d)
        backup_db(cfg)               # end-of-day backup

    def intraday_job():
        if not _in_market_hours():
            return
        # Free tier: live bars carried by yfinance (Massive intraday only on a paid plan).
        from ..state.intraday import run_intraday

        run_intraday(cfg, _today_et())

    def relearn_job():
        from ..predict.relearn import relearn

        relearn(cfg)

    if mode in ("premarket", "both"):
        hh, mm = default_premarket_et(cfg).split(":")
        sched.add_job(premarket_job, CronTrigger(day_of_week="mon-fri", hour=int(hh), minute=int(mm)),
                      id="premarket", name="Pre-market scan")
    if mode in ("intraday", "both"):
        poll = int((cfg.raw.get("run_modes", {}).get("intraday", {}) or {}).get("poll_seconds", 300))
        sched.add_job(intraday_job, IntervalTrigger(seconds=poll),
                      id="intraday", name=f"Intraday loop ({poll}s)")
    if mode in ("postclose", "both"):
        hh, mm = default_postclose_et(cfg).split(":")
        sched.add_job(postclose_job, CronTrigger(day_of_week="mon-fri", hour=int(hh), minute=int(mm)),
                      id="postclose", name="EOD postmortem + backup")
    if mode == "both":
        sched.add_job(relearn_job, CronTrigger(day_of_week="sun", hour=7, minute=0),
                      id="relearn", name="Weekly relearn")
    return sched
