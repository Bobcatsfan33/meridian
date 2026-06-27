"""Honesty constraint engine (ROADMAP §11): block/downgrade weak explanations.

Each rule inspects the structured evidence and may cap the confidence tier and append
an auditable note. Constraints never invent claims — they only demote. The explanation
layer may assert nothing a constraint has not left standing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .scoring import cap_tier


@dataclass(frozen=True)
class ConstraintOutcome:
    tier: str
    notes: list[str] = field(default_factory=list)


def apply(
    *,
    pattern_id: str,
    tier: str,
    cfg_scoring: dict,
    target_abnormality: float,
    sector_abnormality: float | None,
    insufficient_history: bool,
    feeds_ok: bool,
) -> ConstraintOutcome:
    notes: list[str] = []
    out_tier = tier

    # R1: price-only options proxy can never be High (no options data to confirm flow).
    if pattern_id == "options_led_proxy":
        cap = cfg_scoring.get("options_proxy_cap_tier", "Medium")
        new = cap_tier(out_tier, cap)
        if new != out_tier:
            notes.append(f"Price-only proxy (no options layer yet) — capped at {cap}.")
            out_tier = new

    # R2: no high confidence when feeds are delayed/missing/conflicting.
    if not feeds_ok:
        new = cap_tier(out_tier, "Low")
        if new != out_tier:
            notes.append("Feed coverage incomplete — confidence capped at Low.")
            out_tier = new

    # R3: insufficient own-history makes the abnormality grade unreliable.
    if insufficient_history:
        new = cap_tier(out_tier, "Low")
        if new != out_tier:
            notes.append("Insufficient own-name history for a reliable baseline — capped at Low.")
            out_tier = new

    # R4: "sympathy" requires the name not to have clearly led its sector.
    if pattern_id == "sector_sympathy" and sector_abnormality is not None:
        if target_abnormality > 1.5 * sector_abnormality and sector_abnormality > 0:
            new = cap_tier(out_tier, "Medium")
            if new != out_tier:
                notes.append("Target move materially exceeds the sector — may be "
                             "company-specific, not pure sympathy; capped at Medium.")
                out_tier = new

    return ConstraintOutcome(tier=out_tier, notes=notes)
