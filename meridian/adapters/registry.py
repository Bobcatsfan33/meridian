"""Adapter registry: map config blocks to Adapter instances.

Selection precedence:
  - explicit `selected` names (CLI override) win and force-enable those adapters;
  - otherwise every adapter with `enabled: true` in config.adapters runs.
Adapters ship disabled in config.yaml, so a bare run is a no-op until enabled.
"""
from __future__ import annotations

from typing import Iterable

from .base import Adapter
from .edgar import EdgarAdapter
from .earnings import EarningsAdapter
from .finra import FinraAdapter
from .fred import FredAdapter
from .massive import MassiveAdapter
from .news import NewsRssAdapter
from .yfinance import YFinanceAdapter

ADAPTER_CLASSES: dict[str, type[Adapter]] = {
    YFinanceAdapter.name: YFinanceAdapter,
    FredAdapter.name: FredAdapter,
    EdgarAdapter.name: EdgarAdapter,
    NewsRssAdapter.name: NewsRssAdapter,
    EarningsAdapter.name: EarningsAdapter,
    FinraAdapter.name: FinraAdapter,
    MassiveAdapter.name: MassiveAdapter,
}


def build_adapters(
    adapters_cfg: dict[str, dict] | None, selected: Iterable[str] | None = None
) -> list[Adapter]:
    adapters_cfg = adapters_cfg or {}
    selected = list(selected) if selected else None
    if selected:
        unknown = [n for n in selected if n not in ADAPTER_CLASSES]
        if unknown:
            raise ValueError(f"unknown adapter(s): {unknown}. known: {sorted(ADAPTER_CLASSES)}")

    out: list[Adapter] = []
    for name, cls in ADAPTER_CLASSES.items():
        block = adapters_cfg.get(name, {}) or {}
        chosen = (name in selected) if selected is not None else bool(block.get("enabled"))
        if chosen:
            out.append(cls(settings=block))
    out.sort(key=lambda a: (a.priority, a.name))
    return out
