"""State builder (Phase 2): rolling ticker/sector/liquidity state, regime tagging,
and the expected-behavior (beta+macro) baseline that defines the residual denominator.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .builder import build_state as build_state
    from .builder import StateSummary as StateSummary


def __getattr__(name: str):
    if name in {"build_state", "StateSummary"}:
        from . import builder

        return getattr(builder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["build_state", "StateSummary"]
