"""Model tiers exposed to API callers.

The API accepts a tier name ("flash", "pro", "sonnet") rather than a raw
OpenRouter model id. This indirection lets us swap models behind a tier
without breaking clients, and lets each tier carry its own round-cost
estimate for the balance gate.

Tiers are env-overridable via AGAR_TIER_<NAME>_MODEL and
AGAR_TIER_<NAME>_ESTIMATE_CENTS so prod can tune without code changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    name: str                # "flash" | "pro" | "sonnet"
    model: str               # OpenRouter model id, e.g. "google/gemini-2.5-flash"
    estimate_cents: int      # gate value: refuse round if balance < this


_DEFAULTS: dict[str, tuple[str, int]] = {
    # Round-cost estimates calibrated against real 36-agent rounds (same
    # topic, persona_mix=0.5). The gate must always be >= worst-case real
    # cost so balances never go negative. Round up; refund is automatic
    # since cost is reconciled from real usage.cost after the round.
    #
    # Calibration history (2026-06-03):
    #   flash   real 4-16c/round across 5+ sims. 20c gate, ~25% headroom.
    #   smart   claude-haiku-4.5 — replaced gemini-2.5-pro (which was
    #           slow: 15+ min/round, thinking-first model). Haiku is
    #           fast + Anthropic persona-holding. Real cost TBD — initial
    #           40c gate is per-token math, calibrate with a real round.
    #   sonnet  real 41c on a partial 23-comment round; extrapolated full
    #           ~65c. Anthropic models honour the "1-2 paragraphs" rule
    #           more strictly than Gemini = fewer output tokens, so cost
    #           is well below the per-token-math projection. 80c gate.
    "flash":  ("google/gemini-2.5-flash",      20),
    "smart":  ("anthropic/claude-haiku-4.5",   40),
    "sonnet": ("anthropic/claude-sonnet-4.5",  80),
}


def _load_tier(name: str, default_model: str, default_estimate: int) -> Tier:
    env_model = os.environ.get(f"AGAR_TIER_{name.upper()}_MODEL")
    env_estimate = os.environ.get(f"AGAR_TIER_{name.upper()}_ESTIMATE_CENTS")
    return Tier(
        name=name,
        model=env_model or default_model,
        estimate_cents=int(env_estimate) if env_estimate else default_estimate,
    )


TIERS: dict[str, Tier] = {
    name: _load_tier(name, model, estimate)
    for name, (model, estimate) in _DEFAULTS.items()
}

DEFAULT_TIER = os.environ.get("AGAR_DEFAULT_TIER", "flash")


def get_tier(name: str | None) -> Tier:
    """Resolve a tier name to its Tier. Falls back to default for None/unknown."""
    if not name:
        return TIERS[DEFAULT_TIER]
    tier = TIERS.get(name.lower())
    if tier is None:
        # Unknown tier — fall back rather than 400 so old clients still work.
        return TIERS[DEFAULT_TIER]
    return tier
