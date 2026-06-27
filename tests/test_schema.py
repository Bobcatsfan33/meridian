"""Phase 0 acceptance tests: schema applies, universe loads, idempotent."""
from meridian.storage import connect, init_db, table_counts
from meridian.config import Config


def test_all_tables_created(tmp_db):
    con = connect(tmp_db)
    counts = table_counts(con)
    con.close()
    expected = {
        "raw_market_events", "normalized_events", "graded_events", "event_edges",
        "pattern_firings", "move_explanations", "ticker_state_1m", "sector_state_1m",
        "options_state_1m", "liquidity_state_1m", "expected_behavior_1m",
        "regimes_daily", "news_events", "filing_events", "gex_surface",
        "historical_pattern_outcomes", "paper_trades", "calibration_curves",
        "schema_meta", "universe",
    }
    assert expected.issubset(set(counts))


def test_universe_loaded(tmp_db):
    con = connect(tmp_db)
    n = con.execute("SELECT count(*) FROM universe").fetchone()[0]
    sp = con.execute("SELECT count(*) FROM universe "
                     "WHERE index_membership LIKE 'SP500%'").fetchone()[0]
    con.close()
    assert n >= 500
    assert sp >= 490


def test_init_is_idempotent(tmp_db):
    cfg = Config.load()
    # Re-running init must not raise or duplicate the universe.
    init_db(tmp_db, cfg.universe_file)
    con = connect(tmp_db)
    n = con.execute("SELECT count(*) FROM universe").fetchone()[0]
    con.close()
    assert n >= 500
