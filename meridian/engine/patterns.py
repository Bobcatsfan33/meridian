"""Versioned pattern definitions (ROADMAP §10). Declarative YAML in config/patterns/.

A pattern is roles (event selectors) + legs (structural predicates). Patterns carry
NO thresholds — abnormality enters only as a graded weight at match time (hard rule).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class Role:
    name: str
    family: str
    bind: str | None = None       # special binding, e.g. "sector_etf"
    pick: str = "best"            # best (max abnormality) | first (earliest)
    event_type: str | None = None  # optional event_type filter (e.g. ShortGamma)


@dataclass(frozen=True)
class Leg:
    type: str                     # present | absent | concurrent | precedes | contradicts | feature
    role: str | None = None
    a: str | None = None
    b: str | None = None
    family: str | None = None
    feature: str | None = None


@dataclass(frozen=True)
class Pattern:
    id: str
    version: str
    description: str
    roles: tuple[Role, ...]
    legs: tuple[Leg, ...]

    @property
    def rule_id(self) -> str:
        return f"{self.id}@{self.version}"


def load_patterns(directory: pathlib.Path) -> list[Pattern]:
    out: list[Pattern] = []
    for path in sorted(directory.glob("*.yaml")):
        out.append(_parse(yaml.safe_load(path.read_text())))
    return out


def _parse(d: dict[str, Any]) -> Pattern:
    roles = tuple(
        Role(name=k, family=v["family"], bind=v.get("bind"), pick=v.get("pick", "best"),
             event_type=v.get("event_type"))
        for k, v in (d.get("roles") or {}).items()
    )
    legs = tuple(
        Leg(
            type=lg["type"], role=lg.get("role"), a=lg.get("a"), b=lg.get("b"),
            family=lg.get("family"), feature=lg.get("feature"),
        )
        for lg in (d.get("legs") or [])
    )
    return Pattern(
        id=d["id"], version=str(d["version"]), description=d.get("description", ""),
        roles=roles, legs=legs,
    )
