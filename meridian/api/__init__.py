"""Local FastAPI app + single-page dashboard (ROADMAP §13, Phase 7). Local-only,
read-only over DuckDB; reuses the deterministic Jinja renderers (no LLM).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import create_app as create_app


def __getattr__(name: str):
    if name == "create_app":
        from . import app

        return app.create_app
    raise AttributeError(name)
