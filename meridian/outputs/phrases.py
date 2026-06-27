"""Deterministic phrase fragments (ROADMAP §15) keyed to confidence tier and pattern.

A small, fixed lexicon so cards read naturally WITHOUT generating free text. Pure
lookups — identical inputs yield identical phrases (golden-tested). NO LLM.
"""
from __future__ import annotations

# Tier -> the lead-in phrase + the language-softening claim verb (ROADMAP §19).
TIER_PHRASE = {
    "High": "Most supported explanation",
    "Medium": "Plausible explanation",
    "Low": "Tentative read",
    "Unknown": "Insufficient evidence for a confident read",
}
TIER_VERB = {
    "High": "appears to be",
    "Medium": "may be",
    "Low": "could be",
    "Unknown": "is unclear, possibly",
}

# Pattern -> the trader readout (ROADMAP §10 table).
READOUT = {
    "price_before_news": "Price led the tape; the headline reads as late confirmation.",
    "options_led_proxy": "Flow-like expansion with no catalyst — possibly positioning-driven.",
    "sector_sympathy": "Basket / sympathy move; the company-specific signal is weak.",
    "gamma_squeeze": "Mechanical, dealer-hedging driven; headlines demoted to late confirmation.",
}
_DEFAULT_READOUT = "Best-supported structure given the available evidence."


def tier_phrase(tier: str) -> str:
    return TIER_PHRASE.get(tier, TIER_PHRASE["Unknown"])


def tier_verb(tier: str) -> str:
    return TIER_VERB.get(tier, TIER_VERB["Unknown"])


def readout(pattern_id: str) -> str:
    return READOUT.get(pattern_id, _DEFAULT_READOUT)
