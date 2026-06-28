"""Step B1: FINRA equity-flow — parsing, ticker mapping, ATS aggregation, L1 grading."""
from __future__ import annotations

import datetime as dt
import pathlib

from tests.conftest import NOW

from meridian.adapters.base import IngestContext
from meridian.adapters.finra import FinraAdapter
from meridian.engine import featurize_grade as fg
from meridian.ingest.clock import market_close_utc
from meridian.storage import connect

REGSHO = (pathlib.Path(__file__).parent / "fixtures" / "finra_regsho_20260626.txt").read_text()
TARGET = dt.date(2026, 6, 26)


def test_parse_regsho_filters_universe_and_computes_pct():
    recs = {r["symbol"]: r for r in FinraAdapter._parse_regsho(REGSHO, {"AAPL", "AMD", "NVDA"})}
    assert set(recs) == {"AAPL", "AMD"}        # ZZZZ filtered (not in universe); NVDA dropped (total=0)
    assert recs["AAPL"]["as_of"] == "2026-06-26"
    assert abs(recs["AAPL"]["short_pct"] - 520618.0 / 732986.0) < 1e-9


def test_aggregate_ats_latest_week():
    rows = [
        {"summaryStartDate": "2026-06-15", "totalWeeklyShareQuantity": "100"},
        {"summaryStartDate": "2026-06-22", "totalWeeklyShareQuantity": "300"},
        {"summaryStartDate": "2026-06-22", "totalWeeklyShareQuantity": "200"},
    ]
    agg = FinraAdapter._aggregate_ats(rows)
    assert agg == {"week_start": "2026-06-22", "ats_shares": 500.0}


def test_normalize_emits_equity_flow_with_dual_timestamps():
    a = FinraAdapter()
    ctx = IngestContext(trade_date=TARGET, now=NOW, universe=(
        {"symbol": "AAPL", "name": "Apple", "sector": "Information Technology", "index_membership": "SP500"},))
    from meridian.adapters.base import RawEvent
    raw = RawEvent("finra", NOW, "AAPL", {"kind": "short_volume", "as_of": "2026-06-26",
                                          "short_volume": 5.0, "total_volume": 10.0, "short_pct": 0.5})
    ev = a.normalize(raw, ctx)[0]
    assert ev.family == "equity_flow" and ev.event_type == "ShortVolumeSpike"
    assert ev.event_time.date() == TARGET and ev.ingest_time == NOW.astimezone(dt.timezone.utc)


def test_grade_equity_flow_reacts_to_trailing_baseline(tmp_db):
    con = connect(tmp_db)
    close = market_close_utc(TARGET).replace(tzinfo=None)
    # trailing short_pct history ~0.20 for AAPL (25 days before the event)
    for i in range(1, 26):
        con.execute("INSERT INTO equity_flow_state (ticker, ts, short_pct, data_source) VALUES (?,?,?,?)",
                    ["AAPL", close - dt.timedelta(days=i), 0.20, "finra"])
    ev = {"ticker": "AAPL", "event_type": "ShortVolumeSpike", "event_time": close,
          "payload": {"short_pct": 0.55}}
    hi = fg.grade_equity_flow(con, ev, close, 60, 20, 0.5)
    assert hi.method == "own_flow_percentile" and hi.abnormality == 1.0  # 0.55 >> trailing 0.20
    # a value within the baseline grades low
    ev2 = {"ticker": "AAPL", "event_type": "ShortVolumeSpike", "event_time": close,
           "payload": {"short_pct": 0.05}}
    lo = fg.grade_equity_flow(con, ev2, close, 60, 20, 0.5)
    con.close()
    assert lo.abnormality == 0.0 and hi.abnormality > lo.abnormality


def test_grade_equity_flow_insufficient_history(tmp_db):
    con = connect(tmp_db)
    close = market_close_utc(TARGET).replace(tzinfo=None)
    ev = {"ticker": "AAPL", "event_type": "DarkPoolAccumulation", "event_time": close,
          "payload": {"off_exchange_share": 9e6}}
    r = fg.grade_equity_flow(con, ev, close, 60, 20, 0.5)
    con.close()
    assert r.insufficient and r.abnormality == 0.5
