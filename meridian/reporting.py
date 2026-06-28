"""Data accrual + feed-health report (Step D1).

Summarizes what has landed and how healthy each feed is, so the operator can see news +
FINRA + (optional) Massive accruing and the Massive circuit-breaker / throttle state.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .storage import connect


@dataclass
class DataReport:
    by_family: list[tuple[str, int, str]] = field(default_factory=list)      # family, rows, last_date
    by_source: list[tuple[str, int, str]] = field(default_factory=list)      # source, rows, last_ingest
    firings: list[tuple[str, int]] = field(default_factory=list)             # pattern, firings
    outcomes: list[tuple[str, str, int]] = field(default_factory=list)       # pattern, regime, n
    feeds: list[dict[str, Any]] = field(default_factory=list)               # feed health


def build_data_report(cfg: Config) -> DataReport:
    con = connect(cfg.duckdb_path)
    try:
        rep = DataReport()
        rep.by_family = con.execute(
            "SELECT family, count(*), CAST(max(event_time) AS DATE) FROM normalized_events "
            "GROUP BY family ORDER BY family").fetchall()
        rep.by_source = con.execute(
            "SELECT source, count(*), CAST(max(ingest_time) AS DATE) FROM normalized_events "
            "GROUP BY source ORDER BY source").fetchall()
        rep.firings = con.execute(
            "SELECT pattern_id, count(*) FROM pattern_firings GROUP BY pattern_id "
            "ORDER BY 2 DESC").fetchall()
        rep.outcomes = con.execute(
            "SELECT pattern_id, regime_label, count(*) FROM historical_pattern_outcomes "
            "GROUP BY pattern_id, regime_label ORDER BY pattern_id, regime_label").fetchall()
    finally:
        con.close()
    rep.feeds = _feed_health(cfg)
    return rep


def _feed_health(cfg: Config) -> list[dict[str, Any]]:
    adapters = cfg.raw.get("adapters", {}) or {}
    out: list[dict[str, Any]] = []
    for name in ("yfinance", "fred", "edgar", "news_rss", "finra", "massive"):
        block = adapters.get(name, {}) or {}
        row: dict[str, Any] = {"feed": name, "enabled": bool(block.get("enabled"))}
        if name == "massive":
            key = bool(os.environ.get(block.get("api_key_env", "MASSIVE_API_KEY")))
            row["key_present"] = key
            row["breaker"] = "n/a"
            row["throttle_wait_s"] = 0.0
            if block.get("enabled") and key:
                try:
                    from .adapters.massive import client_from_config

                    c = client_from_config(cfg)
                    if c is not None:
                        row["breaker"] = c.breaker.state
                        row["throttle_wait_s"] = round(c.bucket.total_wait, 1)
                except Exception:
                    pass
        out.append(row)
    return out
