import datetime as dt
import json
import os
import pathlib

import pytest

from meridian.config import Config
from meridian.storage import init_db

GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"

# Fixed clocks so ingest_time and golden output are deterministic.
NOW = dt.datetime(2026, 6, 27, 13, 30, 0, tzinfo=dt.timezone.utc)
TRADE_DATE = dt.date(2026, 6, 26)


@pytest.fixture()
def tmp_db(tmp_path) -> pathlib.Path:
    cfg = Config.load()
    db = tmp_path / "test.duckdb"
    init_db(db, cfg.universe_file)
    return db


@pytest.fixture()
def sample_ctx():
    """A small, network-free IngestContext for normalization tests."""
    from meridian.adapters.base import IngestContext

    universe = (
        {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Information Technology", "index_membership": "SP500"},
        {"symbol": "AMD", "name": "Advanced Micro Devices", "sector": "Information Technology", "index_membership": "SP500"},
        {"symbol": "NVDA", "name": "NVIDIA Corporation", "sector": "Information Technology", "index_membership": "SP500"},
    )
    etfs = (
        {"symbol": "SPY", "role": "index", "description": "S&P 500"},
        {"symbol": "XLK", "role": "sector", "description": "Information Technology"},
        {"symbol": "^VIX", "role": "macro", "description": "Volatility index"},
    )
    return IngestContext(trade_date=TRADE_DATE, now=NOW, universe=universe, etfs=etfs)


def golden(name: str, produced) -> None:
    """Compare `produced` to tests/golden/<name>.json. Set REGEN_GOLDEN=1 to rewrite."""
    GOLDEN_DIR.mkdir(exist_ok=True)
    path = GOLDEN_DIR / f"{name}.json"
    blob = json.dumps(produced, indent=2, sort_keys=True, default=str)
    if os.environ.get("REGEN_GOLDEN") or not path.exists():
        path.write_text(blob + "\n")
    expected = path.read_text().rstrip("\n")
    assert blob == expected, f"golden mismatch for {name} (REGEN_GOLDEN=1 to update)"


def event_to_dict(e) -> dict:
    """Canonical, stable serialization of a NormalizedEvent for golden comparison."""
    return {
        "event_id": e.event_id,
        "event_time": e.event_time.astimezone(dt.timezone.utc).isoformat(),
        "ingest_time": e.ingest_time.astimezone(dt.timezone.utc).isoformat(),
        "ticker": e.ticker,
        "event_type": e.event_type,
        "family": e.family,
        "source": e.source,
        "confidence": e.confidence,
        "sector": e.sector,
        "related_symbols": list(e.related_symbols),
        "parent_event_id": e.parent_event_id,
        "payload": e.payload,
        "latency_seconds": e.latency_seconds,
    }
