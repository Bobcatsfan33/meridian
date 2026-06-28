"""`meridian demo` completes offline and produces >=1 card; idempotent + deterministic."""
from __future__ import annotations

from meridian.demo import DEMO_DATE, run_demo


def test_demo_runs_offline_and_produces_cards(tmp_path):
    db = str(tmp_path / "demo.duckdb")
    res = run_demo(db_path=db)
    assert res.date == DEMO_DATE
    assert res.steps == ["seed", "featurize", "match", "explanations+postmortem"]
    assert res.n_events >= 1 and res.n_firings >= 1
    assert res.n_cards >= 1, "demo must produce at least one card"
    assert res.top and res.top[0][0] in {"AAPL", "MSFT", "NVDA"}


def test_demo_is_idempotent(tmp_path):
    db = str(tmp_path / "demo.duckdb")
    a = run_demo(db_path=db)
    b = run_demo(db_path=db)            # re-run on the same DB
    assert (a.n_events, a.n_firings, a.n_cards) == (b.n_events, b.n_firings, b.n_cards)
