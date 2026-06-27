"""Data adapters. Common interface; free-first, then Robinhood MCP, then paid.

Every adapter turns a source's raw payloads into the canonical typed event shape
(ROADMAP §7) with dual timestamps. Adapters ship DISABLED in config.yaml; the
runtime enables them per `config.adapters.<name>.enabled` (or a CLI override).
"""
from .base import Adapter, RawEvent, NormalizedEvent, make_event_id, FAMILIES
from .registry import build_adapters, ADAPTER_CLASSES

__all__ = [
    "Adapter",
    "RawEvent",
    "NormalizedEvent",
    "make_event_id",
    "FAMILIES",
    "build_adapters",
    "ADAPTER_CLASSES",
]
