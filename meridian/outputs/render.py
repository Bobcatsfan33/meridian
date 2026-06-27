"""Jinja rendering (ROADMAP §15). DETERMINISTIC, no LLM. Templates may print only
fields from the structured evidence object. Identical input -> identical output.
"""
from __future__ import annotations

import math
import pathlib
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATES = pathlib.Path(__file__).with_name("templates")


def _pct(x: Any, signed: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        undefined=StrictUndefined,        # printing a missing field is an error, not a blank
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )
    env.filters["pct"] = lambda x: _pct(x)
    env.filters["spct"] = lambda x: _pct(x, signed=True)
    return env


def render_card(evidence: dict) -> str:
    return _env().get_template("card.j2").render(ev=evidence)


def render_scanner(evidences: list[dict], date: str) -> str:
    rows = sorted(
        evidences, key=lambda e: e["confidence"]["value"], reverse=True
    )
    return _env().get_template("scanner.j2").render(rows=rows, date=date, n=len(rows))


def render_postmortem(context: dict) -> str:
    return _env().get_template("postmortem.j2").render(**context)
