"""Phase 1 ingestion: adapters -> raw payloads -> typed normalized events.

Import the pipeline lazily to avoid a cycle: adapters.base imports ingest.clock,
so this package must not eagerly import the pipeline (which imports adapters).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # for type checkers only; no runtime import
    from .pipeline import IngestResult as IngestResult
    from .pipeline import run_ingest as run_ingest

__all__ = ["run_ingest", "IngestResult"]


def __getattr__(name: str):
    if name in {"run_ingest", "IngestResult"}:
        from . import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
