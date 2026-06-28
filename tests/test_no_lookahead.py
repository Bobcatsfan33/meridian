"""Issue 2: adversarial no-lookahead guards (ROADMAP §18).

(1) Behavioral: future state (event_time > t) must never move a feature graded at t;
    and the same-period close must be EXCLUDED from its own trailing baseline (so a
    `< close` -> `<= close` regression changes the result and fails this test).
(2) Static: feature reads against time-series tables in engine/state/predict/outputs
    must bound trailing windows with `<` (not `<=`), unless explicitly allow-listed.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re

from meridian.config import Config
from meridian.engine.featurize import featurize
from meridian.ingest.clock import market_close_utc
from meridian.storage import connect

TARGET = dt.date(2026, 6, 26)
CLOSE = market_close_utc(TARGET).replace(tzinfo=None)
ROOT = pathlib.Path(__file__).resolve().parents[1] / "meridian"


def _seed(con, today_ret: float, trailing: list[float]):
    con.execute("INSERT INTO normalized_events (event_id,event_time,ingest_time,ticker,event_type,"
                "family,source,confidence) VALUES (?,?,?,?,?,?,?,?)",
                ["p_x", CLOSE, CLOSE, "X", "DailyBar", "price_volume", "test", 0.95])
    con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)", ["X", CLOSE, today_ret])
    for i, r in enumerate(trailing, start=1):
        con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)",
                    ["X", CLOSE - dt.timedelta(days=i), r])
    con.execute("INSERT INTO regimes_daily (trade_date, regime_label, regime_tags) VALUES (?,?,?)",
                [TARGET, "mid_vol_range", ["mid_vol", "range"]])


def _abn(con):
    return con.execute("SELECT abnormality FROM graded_events WHERE event_id='p_x'").fetchone()[0]


def test_future_data_does_not_leak(tmp_db):
    cfg = Config.load()
    con = connect(tmp_db)
    _seed(con, today_ret=0.05, trailing=[0.01] * 24 + [0.20])
    featurize(con, cfg, TARGET)
    before = _abn(con)
    # adversarial: an extreme state row strictly AFTER the close must not move t's grade
    con.execute("INSERT INTO ticker_state_1m (ticker, ts, ret_1m) VALUES (?,?,?)",
                ["X", CLOSE + dt.timedelta(days=1), 9.99])
    featurize(con, cfg, TARGET)
    after = _abn(con)
    con.close()
    assert before == after, "future data leaked into a feature graded at t"


def test_same_period_close_excluded_from_baseline(tmp_db):
    # today's 0.05 vs trailing {0.01 x24, 0.20}: strict `<` -> 24/25 = 0.96.
    # Flipping the trailing query to `<=` would add today's 0.05 -> 25/26 ≈ 0.9615 and break this.
    cfg = Config.load()
    con = connect(tmp_db)
    _seed(con, today_ret=0.05, trailing=[0.01] * 24 + [0.20])
    featurize(con, cfg, TARGET)
    abn = _abn(con)
    con.close()
    assert abs(abn - 0.96) < 1e-9


def test_static_guard_feature_reads_use_strict_bound():
    """No `<=` on a time column in a feature read unless DELETE/UPDATE/INSERT or
    annotated `lookahead-ok`."""
    time_le = re.compile(r"(ts|window_start|trade_date|event_time)\s*<=")
    offenders: list[str] = []
    for sub in ("engine", "state", "predict", "outputs"):
        for path in (ROOT / sub).rglob("*.py"):
            lines = path.read_text().splitlines()
            for i, line in enumerate(lines):
                if not time_le.search(line):
                    continue
                up = line.upper()
                if any(k in up for k in ("DELETE", "UPDATE", "INSERT")):
                    continue
                context = "\n".join(lines[max(0, i - 7): i + 2])
                if "lookahead-ok" in context:
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{i + 1}: {line.strip()}")
    assert not offenders, "feature reads must use `<` (or annotate lookahead-ok):\n" + "\n".join(offenders)
