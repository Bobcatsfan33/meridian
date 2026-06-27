"""EOD postmortem context (ROADMAP §13 C). Aggregates the day's explanations into
scoreboard + thematic sections. Every section surfaces the unexplained residual.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import Config
from ..storage import connect


def build_context(cfg: Config, target_date: dt.date, evidences: list[dict]) -> dict[str, Any]:
    con = connect(cfg.duckdb_path)
    try:
        regime = con.execute(
            "SELECT regime_label, regime_tags FROM regimes_daily WHERE trade_date=?", [target_date]
        ).fetchone()
        firing_stats = con.execute(
            "SELECT pattern_id, count(*), avg(completeness) FROM pattern_firings "
            "WHERE CAST(window_start AS DATE)=? GROUP BY pattern_id", [target_date]
        ).fetchall()
    finally:
        con.close()

    resid_by_pat: dict[str, list[float]] = {}
    for e in evidences:
        resid_by_pat.setdefault(e["pattern"]["id"], []).append(e["unexplained_residual"])

    scoreboard = []
    for pat, count, mean_comp in sorted(firing_stats):
        resids = resid_by_pat.get(pat, [])
        scoreboard.append({
            "pattern": pat, "count": count, "mean_completeness": mean_comp,
            "mean_residual": (sum(resids) / len(resids)) if resids else float("nan"),
        })

    def by_pattern(pid: str, limit: int = 10) -> list[dict]:
        rows = [e for e in evidences if e["pattern"]["id"] == pid]
        rows.sort(key=lambda e: e["confidence"]["value"], reverse=True)
        return rows[:limit]

    return {
        "date": target_date.isoformat(),
        "regime_label": regime[0] if regime else "",
        "regime_tags": list(regime[1]) if regime and regime[1] else [],
        "n_explained": len(evidences),
        "scoreboard": scoreboard,
        "flow_like": by_pattern("options_led_proxy"),
        "price_before_news": by_pattern("price_before_news"),
        "sympathy": by_pattern("sector_sympathy"),
    }
