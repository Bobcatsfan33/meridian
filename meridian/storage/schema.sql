-- Meridian DuckDB schema (Phase 0). Idempotent: safe to run repeatedly.
-- Tables follow ROADMAP.md Section 8. Rolling-state tables are populated by the
-- state builder (Phase 2); event/edge/explanation tables by the engine (P3-P6).

-- ---------- raw + normalized event pipeline ----------
CREATE TABLE IF NOT EXISTS raw_market_events (
    event_id      VARCHAR PRIMARY KEY,
    ingest_time   TIMESTAMP NOT NULL,
    source        VARCHAR NOT NULL,
    ticker        VARCHAR,
    payload       JSON
);

CREATE TABLE IF NOT EXISTS normalized_events (
    event_id        VARCHAR PRIMARY KEY,
    event_time      TIMESTAMP NOT NULL,   -- source clock, aligned
    ingest_time     TIMESTAMP NOT NULL,   -- when we received it
    ticker          VARCHAR,
    event_type      VARCHAR NOT NULL,
    family          VARCHAR NOT NULL,     -- price_volume|options_flow|dealer_pos|news|filing|sector_peer|macro|liquidity|attention
    source          VARCHAR NOT NULL,
    confidence      DOUBLE,               -- data-quality / source reliability
    sector          VARCHAR,
    related_symbols VARCHAR[],
    parent_event_id VARCHAR,              -- reserved: membership in complex event
    payload         JSON
);

CREATE TABLE IF NOT EXISTS graded_events (
    event_id     VARCHAR PRIMARY KEY,
    event_time   TIMESTAMP NOT NULL,
    ticker       VARCHAR,
    event_type   VARCHAR NOT NULL,
    abnormality  DOUBLE,                  -- [0,1] vs this name's own regime baseline (L1)
    regime_tags  VARCHAR[],
    confidence   DOUBLE,
    payload      JSON
);

-- ---------- evidence graph (AUDIT) ----------
CREATE TABLE IF NOT EXISTS event_edges (
    edge_id      VARCHAR PRIMARY KEY,
    src_event_id VARCHAR NOT NULL,
    dst_event_id VARCHAR NOT NULL,
    ticker       VARCHAR,
    edge_type    VARCHAR NOT NULL,        -- precedes|concurrent|independent|contradicts
    lag_seconds  DOUBLE,
    test_stat    DOUBLE,                  -- lead-lag / Granger / transfer-entropy score
    test_pvalue  DOUBLE,                  -- edge trusted only if < causal_test_alpha
    confidence   DOUBLE,
    rule_id      VARCHAR,                 -- which named, versioned pattern asserted it
    created_at   TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pattern_firings (
    firing_id     VARCHAR PRIMARY KEY,
    ticker        VARCHAR NOT NULL,
    pattern_id    VARCHAR NOT NULL,       -- named, versioned pattern
    pattern_ver   VARCHAR,
    window_start  TIMESTAMP,
    window_end    TIMESTAMP,
    completeness  DOUBLE,                 -- [0,1], never boolean
    confidence    DOUBLE,                 -- L3 calibrated
    regime_tags   VARCHAR[],
    created_at    TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS move_explanations (
    explanation_id     VARCHAR PRIMARY KEY,
    ticker             VARCHAR NOT NULL,
    window_start       TIMESTAMP,
    window_end         TIMESTAMP,
    abnormal_move_pct  DOUBLE,            -- denominator: move beyond expected behavior
    driver_attribution JSON,             -- [{driver, weight, inputs{...}}]
    unexplained_residual DOUBLE,         -- enforced: attribution + residual = 1.0
    invalidation       VARCHAR,
    confidence_tier    VARCHAR,          -- High|Medium|Low|Unknown
    evidence_object    JSON,             -- single source of truth for the card
    created_at         TIMESTAMP DEFAULT now()
);

-- ---------- rolling state + baselines (Phase 2) ----------
CREATE TABLE IF NOT EXISTS ticker_state_1m (
    ticker VARCHAR, ts TIMESTAMP, close DOUBLE, vwap DOUBLE,
    rel_volume DOUBLE, atr DOUBLE, ret_1m DOUBLE
);
CREATE TABLE IF NOT EXISTS sector_state_1m (
    sector VARCHAR, ts TIMESTAMP, etf VARCHAR, etf_ret_1m DOUBLE, breadth DOUBLE
);
CREATE TABLE IF NOT EXISTS options_state_1m (
    ticker VARCHAR, ts TIMESTAMP, iv DOUBLE, iv_pctile DOUBLE,
    net_gex DOUBLE, gamma_flip DOUBLE, call_wall DOUBLE, put_wall DOUBLE
);
CREATE TABLE IF NOT EXISTS liquidity_state_1m (
    ticker VARCHAR, ts TIMESTAMP, spread_bps DOUBLE, depth DOUBLE, halted BOOLEAN
);
CREATE TABLE IF NOT EXISTS expected_behavior_1m (
    ticker VARCHAR, ts TIMESTAMP, expected_ret DOUBLE, beta DOUBLE,
    macro_component DOUBLE, abnormal_ret DOUBLE
);
CREATE TABLE IF NOT EXISTS regimes_daily (
    trade_date DATE, vix_level DOUBLE, vix_term DOUBLE,
    index_trend VARCHAR, breadth DOUBLE, regime_label VARCHAR,
    regime_tags VARCHAR[]
);

-- ---------- typed source event side-tables ----------
CREATE TABLE IF NOT EXISTS news_events (
    event_id VARCHAR PRIMARY KEY, event_time TIMESTAMP, ticker VARCHAR,
    headline VARCHAR, source VARCHAR, sentiment DOUBLE, topic VARCHAR
);
CREATE TABLE IF NOT EXISTS filing_events (
    event_id VARCHAR PRIMARY KEY, event_time TIMESTAMP, ticker VARCHAR,
    form_type VARCHAR, accession VARCHAR, url VARCHAR
);
-- equity-flow state (Part B): FINRA short-volume + dark-pool, the L1 baseline source
CREATE TABLE IF NOT EXISTS equity_flow_state (
    ticker VARCHAR, ts TIMESTAMP,
    short_pct DOUBLE,             -- ShortVolume / TotalVolume (Reg SHO daily)
    off_exchange_share DOUBLE,    -- weekly ATS (dark-pool) share volume
    data_source VARCHAR
);

CREATE TABLE IF NOT EXISTS gex_surface (
    ticker VARCHAR, ts TIMESTAMP, strike DOUBLE, expiry DATE,
    gamma DOUBLE, open_interest DOUBLE, dealer_gamma DOUBLE,
    data_source VARCHAR              -- live (real chain) | fixture (synthetic proxy)
);

-- ---------- predictive engine (Phase 6) ----------
CREATE TABLE IF NOT EXISTS historical_pattern_outcomes (
    firing_id VARCHAR, pattern_id VARCHAR, regime_label VARCHAR,
    horizon VARCHAR, fwd_return DOUBLE, mfe DOUBLE, mae DOUBLE
);
CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id VARCHAR PRIMARY KEY, firing_id VARCHAR, ticker VARCHAR,
    entry_ts TIMESTAMP, exit_ts TIMESTAMP, entry_px DOUBLE, exit_px DOUBLE,
    ret DOUBLE, win BOOLEAN
);
CREATE TABLE IF NOT EXISTS calibration_curves (
    pattern_id VARCHAR, regime_label VARCHAR, bin_lo DOUBLE, bin_hi DOUBLE,
    predicted DOUBLE, realized DOUBLE, n INTEGER
);

-- ---------- schema metadata ----------
CREATE TABLE IF NOT EXISTS schema_meta (
    key VARCHAR PRIMARY KEY, value VARCHAR
);
