"""Population assembly for API conversations.

Fixed population of 36:
  - 18 spicy (always present): 12 SARC + 6 HN
  - 18 flavor slots: split by persona_mix between adversarial and creative

persona_mix 0.0 = 18 spicy + 18 creative
persona_mix 0.5 = 18 spicy + 9 adversarial + 9 creative
persona_mix 1.0 = 18 spicy + 18 adversarial
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

PERSONAS_DIR = Path(__file__).parent.parent / "personas"

SARC_PATH = os.environ.get("AGAR_SARC_PATH", str(PERSONAS_DIR / "personas_sarc.jsonl"))
HN_SPICY_PATH = os.environ.get("AGAR_HN_SPICY_PATH", str(PERSONAS_DIR / "personas_hn_spicy.jsonl"))
ADVERSARIAL_PATH = os.environ.get("AGAR_ADVERSARIAL_PATH", str(PERSONAS_DIR / "personas_adversarial.jsonl"))
CREATIVE_PATH = os.environ.get("AGAR_CREATIVE_PATH", str(PERSONAS_DIR / "personas_creative.jsonl"))

FLAVOR_SLOTS = 18  # non-spicy half


def _load_jsonl(path: str) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    profiles = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                profiles.append(json.loads(line))
    return profiles


def assemble_population(persona_mix: float, seed: int | None = None) -> list[dict]:
    """Build fixed-size population: 18 spicy + 18 flavor slots.

    Args:
        persona_mix: 0.0=all creative, 1.0=all adversarial.
        seed: Random seed for reproducibility.

    Returns:
        List of 36 profile dicts.
    """
    rng = random.Random(seed)

    # Always-present spicy layer (all 18)
    selected = _load_jsonl(SARC_PATH) + _load_jsonl(HN_SPICY_PATH)

    # Split flavor slots by ratio
    adversarial_pool = _load_jsonl(ADVERSARIAL_PATH)
    creative_pool = _load_jsonl(CREATIVE_PATH)

    adv_count = round(FLAVOR_SLOTS * persona_mix)
    cre_count = FLAVOR_SLOTS - adv_count

    rng.shuffle(adversarial_pool)
    rng.shuffle(creative_pool)

    selected.extend(adversarial_pool[:adv_count])
    selected.extend(creative_pool[:cre_count])

    rng.shuffle(selected)
    return selected
