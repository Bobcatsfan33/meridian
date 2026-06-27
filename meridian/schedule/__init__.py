"""Scheduling (APScheduler). Phase 4 ships the post-close job + a day pipeline
runner; Phase 7 adds the pre-market scan and the intraday polling loop.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jobs import run_postclose as run_postclose


def __getattr__(name: str):
    if name == "run_postclose":
        from . import jobs

        return jobs.run_postclose
    raise AttributeError(name)
