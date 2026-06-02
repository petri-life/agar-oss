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
    # Round-cost estimates are calibrated to the observed cost of a 36-agent
    # round including the late-round context-growth premium. Round up to keep
    # the balance non-negative even on bad days. Tune per actual data:
    "flash":  ("google/gemini-2.5-flash",       20),   # observed 4-16¢/round
    "pro":    ("google/gemini-2.5-pro",         70),   # ~4x flash
    "sonnet": ("anthropic/claude-sonnet-4.5",  170),   # ~8-9x flash
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
