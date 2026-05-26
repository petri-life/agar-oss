"""Stratified population sampling from scored user profiles.

Loads MobileRec scored JSONL and samples a population stratified by
dominant friction tag. Minority tags are oversampled to ensure every
tag present in the source data is represented in the sample.
"""

import json
import math
import random
from collections import defaultdict
from pathlib import Path


FRICTION_TAGS = [
    "broken-core",
    "feature-noise",
    "onboarding-friction",
    "support-failure",
    "trust-failure",
    "wrong-fit",
    "value-unclear",
    "switching-cost",
    "platform-dependency",
    "habit-gap",
]


def profile_quality(profile: dict) -> float:
    """Score a profile by richness and behavioral spread.

    Combines:
    - Review count (log-scaled): more reviews = richer voice
    - Score entropy: spread across friction dimensions = more nuanced agent
    - Non-zero tag count: agents with many active dimensions are more expressive

    Returns a float in roughly [0, 1] suitable for sorting/weighting.
    """
    scores = profile.get("scores", {})
    num_reviews = len(profile.get("reviews", []))

    # Review count component (log-scaled, saturates around 20)
    review_score = math.log1p(num_reviews) / math.log1p(20)

    # Shannon entropy of non-zero scores (normalized)
    values = [v for v in scores.values() if v > 0]
    if len(values) < 2:
        entropy_score = 0.0
    else:
        total = sum(values)
        probs = [v / total for v in values]
        raw_entropy = -sum(p * math.log2(p) for p in probs)
        max_entropy = math.log2(len(values))
        entropy_score = raw_entropy / max_entropy if max_entropy > 0 else 0.0

    # Non-zero tag count (normalized to 10 tags)
    nonzero_score = sum(1 for v in scores.values() if v > 0) / len(FRICTION_TAGS)

    return 0.4 * review_score + 0.4 * entropy_score + 0.2 * nonzero_score


def load_profiles(path: str | Path) -> list[dict]:
    """Load scored profiles from JSONL file.

    Handles both legacy format (mobilerec_scored.jsonl) and bundle format
    (export_bundles.jsonl). Normalizes to a common shape:
      uid, dominant, scores (flat dict), reviews, voice (optional)
    """
    profiles = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)

            # Resolved persona format (from agar setup)
            if "role" in raw and "context" in raw:
                raw.setdefault("dominant", raw["role"])
                profiles.append(raw)
            # HN/spicy persona format: role or tone-based, comments only
            elif "persona_id" in raw:
                profile = {
                    "uid": raw["persona_id"],
                    "dominant": raw.get("role") or raw.get("tone", "unknown"),
                    "scores": {},
                    "reviews": [],
                    "voice": raw.get("comments", []),
                }
                if "directive" in raw:
                    profile["directive"] = raw["directive"]
                profiles.append(profile)
            # Bundle format: scores.friction nested, primary_vector, voice
            elif "primary_vector" in raw:
                profiles.append({
                    "uid": raw["uid"],
                    "dominant": raw["primary_vector"],
                    "scores": raw.get("scores", {}).get("friction", {}),
                    "reviews": raw.get("reviews", []),
                    "voice": raw.get("voice", []),
                })
            else:
                # Legacy format: flat scores, dominant
                profiles.append(raw)

    return profiles


SPICY_TAGS = {"joker", "troll", "fanboy", "unhinged", "denier", "drifter"}


def sample_population(
    profiles: list[dict],
    n: int,
    seed: int | None = None,
    spicy_ratio: float = 0.3,
) -> list[dict]:
    """Sample n profiles with guaranteed spicy representation.

    Spicy personas (joker, troll, fanboy, unhinged, denier, drifter) get
    at least 1 each if available, up to spicy_ratio of the population.
    Remaining slots filled from the main pool with tag diversity.

    Args:
        profiles: Full list of profile dicts (mixed sources).
        n: Target population size.
        seed: Random seed for reproducibility.
        spicy_ratio: Max fraction of population that can be spicy (default 0.15).

    Returns:
        List of n profile dicts.
    """
    if n > len(profiles):
        raise ValueError(
            f"Requested {n} profiles but only {len(profiles)} available"
        )

    rng = random.Random(seed)

    spicy = [p for p in profiles if p["dominant"] in SPICY_TAGS]
    main = [p for p in profiles if p["dominant"] not in SPICY_TAGS]

    # If no main pool, skip ratio logic — just sample from what's available
    if not main:
        rng.shuffle(spicy)
        return spicy[:n]
    if not spicy:
        rng.shuffle(main)
        return main[:n]

    selected: list[dict] = []

    # Phase 1: pick spicy — at least 1 per type, up to spicy_ratio * n
    max_spicy = max(len(SPICY_TAGS), int(n * spicy_ratio))
    by_spicy_tag: dict[str, list[dict]] = defaultdict(list)
    for p in spicy:
        by_spicy_tag[p["dominant"]].append(p)

    for tag in by_spicy_tag:
        pool = list(by_spicy_tag[tag])
        rng.shuffle(pool)
        selected.append(pool[0])  # guarantee 1

    # Fill remaining spicy slots if budget allows
    remaining_spicy = [p for p in spicy if p not in selected]
    rng.shuffle(remaining_spicy)
    spicy_budget = max_spicy - len(selected)
    if spicy_budget > 0:
        selected.extend(remaining_spicy[:spicy_budget])

    # Phase 2: fill rest from main pool with tag diversity
    slots = n - len(selected)
    if slots > 0:
        by_tag: dict[str, list[dict]] = defaultdict(list)
        for p in main:
            by_tag[p["dominant"]].append(p)

        # Sort each tag bucket by quality
        for tag in by_tag:
            by_tag[tag].sort(key=profile_quality, reverse=True)

        # Round-robin 1 per tag, then fill remainder
        tags = list(by_tag.keys())
        rng.shuffle(tags)
        main_selected: list[dict] = []
        remaining: list[dict] = []

        for tag in tags:
            pool = by_tag[tag]
            if pool and len(main_selected) < slots:
                rng.shuffle(pool)
                main_selected.append(pool[0])
                remaining.extend(pool[1:])
            else:
                remaining.extend(pool)

        # Fill remaining slots
        rng.shuffle(remaining)
        main_selected.extend(remaining[:slots - len(main_selected)])
        selected.extend(main_selected)

    rng.shuffle(selected)
    return selected[:n]
