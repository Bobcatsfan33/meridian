"""Golden test for the BYOD example adapter (docs/examples/csv_adapter.py).

Proves the invariants a tester must preserve when copying it: event_time parses to UTC,
family validates, ingest_time >= event_time (no-lookahead), and dedup via stable event_id.
The adapter lives in docs/examples (the file a tester copies), so it's loaded by path.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib

from tests.conftest import NOW

from meridian.adapters.base import IngestContext

_EX = pathlib.Path(__file__).resolve().parents[1] / "docs" / "examples"
SAMPLE = _EX / "sample_events.csv"


def _load_csv_adapter():
    spec = importlib.util.spec_from_file_location("csv_adapter_example", _EX / "csv_adapter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CsvAdapter


def _ctx(trade_date=dt.date(2026, 6, 26)):
    universe = (
        {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Information Technology", "index_membership": "SP500"},
        {"symbol": "MSFT", "name": "Microsoft", "sector": "Information Technology", "index_membership": "SP500"},
        {"symbol": "NVDA", "name": "NVIDIA", "sector": "Information Technology", "index_membership": "SP500"},
    )
    return IngestContext(trade_date=trade_date, now=NOW, universe=universe)


def test_csv_adapter_end_to_end_invariants():
    CsvAdapter = _load_csv_adapter()
    a = CsvAdapter(settings={"csv_path": str(SAMPLE)})
    ctx = _ctx()
    raws, events = a.run(ctx)
    assert events, "expected events for 2026-06-26"

    for e in events:
        # event_time parsed to tz-aware UTC
        assert e.event_time.tzinfo is not None and e.event_time.utcoffset() == dt.timedelta(0)
        # family validates (NormalizedEvent would have raised otherwise)
        from meridian.adapters.base import FAMILIES
        assert e.family in FAMILIES
        # dual timestamps, no-lookahead: received no earlier than it happened
        assert e.ingest_time >= e.event_time
        assert e.latency_seconds >= 0

    # only the trade_date's rows are kept (the 2026-06-25 rows are excluded)
    assert {e.event_time.date() for e in events} == {dt.date(2026, 6, 26)}
    fams = {e.family for e in events}
    assert {"price_volume", "news", "filing", "equity_flow"} <= fams  # multiple families mapped


def test_csv_adapter_dedup_is_stable():
    CsvAdapter = _load_csv_adapter()
    a = CsvAdapter(settings={"csv_path": str(SAMPLE)})
    ctx = _ctx()
    _raws, events = a.run(ctx)
    ids = [e.event_id for e in events]
    assert len(ids) == len(set(ids))  # no dupes within a run

    # re-normalizing the SAME raw row yields the SAME event_id (idempotent upsert)
    raw0 = a.fetch(ctx)[0]
    assert a.normalize(raw0, ctx)[0].event_id == a.normalize(raw0, ctx)[0].event_id


def test_csv_adapter_rejects_unknown_family(tmp_path):
    CsvAdapter = _load_csv_adapter()
    bad = tmp_path / "bad.csv"
    bad.write_text("event_time,ticker,event_type,family,value,payload_json\n"
                   "2026-06-26T20:00:00+00:00,AAPL,Weird,not_a_family,1,\n")
    a = CsvAdapter(settings={"csv_path": str(bad)})
    ctx = _ctx()
    raw = a.fetch(ctx)[0]
    try:
        a.normalize(raw, ctx)
        raised = False
    except ValueError:
        raised = True
    assert raised, "an unknown family must be rejected by NormalizedEvent validation"
