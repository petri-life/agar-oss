"""Persona setup — compose source artifacts into a structured persona file.

Reads a YAML manifest listing source files + counts, samples and resolves
personas, outputs a single JSONL file ready for simulation.

The output file is the contract between setup and sim — fully resolved,
editable, inspectable.
"""

import json
import logging
import random
from collections import defaultdict
from pathlib import Path

import yaml

from sim.sampler import load_profiles, profile_quality

log = logging.getLogger("agar.setup")


def load_manifest(manifest_path: str | Path) -> dict:
    """Load a YAML manifest describing persona sources."""
    with open(manifest_path) as f:
        return yaml.safe_load(f)


def _resolve_persona(raw: dict) -> dict:
    """Normalize a raw profile into the standard persona shape."""
    return {
        "uid": raw.get("uid", "unknown"),
        "role": raw.get("dominant", "unknown"),
        "directive": raw.get("directive", ""),
        "voice": raw.get("voice", []),
        "reviews": raw.get("reviews", []),
        "context": "full",
    }


def _sample_from_source(
    profiles: list[dict],
    count: int,
    required: bool,
    rng: random.Random,
) -> list[dict]:
    """Sample `count` personas from a source, guaranteeing type diversity if required."""
    if required:
        # Guarantee at least 1 per type, fill remaining
        by_type: dict[str, list[dict]] = defaultdict(list)
        for p in profiles:
            by_type[p["dominant"]].append(p)

        selected = []
        remaining = []
        for typ, pool in by_type.items():
            rng.shuffle(pool)
            selected.append(pool[0])
            remaining.extend(pool[1:])

        slots = count - len(selected)
        if slots > 0:
            rng.shuffle(remaining)
            selected.extend(remaining[:slots])

        return selected[:count]
    else:
        # Quality-weighted sample
        profiles.sort(key=profile_quality, reverse=True)
        # Take from top half, shuffle for variety
        pool = profiles[:max(count * 2, len(profiles))]
        rng.shuffle(pool)
        return pool[:count]


def generate_personas(
    manifest_path: str | Path,
    seed: int = 42,
    comments_only_ratio: float = 0.3,
) -> list[dict]:
    """Generate personas from a manifest.

    Args:
        manifest_path: Path to YAML manifest.
        seed: Random seed.
        comments_only_ratio: Fraction of personas that only see comments,
            not the original post. Simulates people who read the thread
            without reading the article.

    Returns:
        List of resolved persona dicts.
    """
    manifest = load_manifest(manifest_path)
    rng = random.Random(seed)
    all_personas = []

    for source in manifest["sources"]:
        file_path = source["file"]
        count = source.get("count", 10)
        required = source.get("required", False)

        profiles = load_profiles(file_path)
        if not profiles:
            log.warning("No profiles in %s, skipping", file_path)
            continue

        sampled = _sample_from_source(profiles, count, required, rng)
        resolved = [_resolve_persona(p) for p in sampled]

        log.info("Loaded %d personas from %s (%d available)",
                 len(resolved), file_path, len(profiles))
        all_personas.extend(resolved)

    rng.shuffle(all_personas)

    # Assign comments-only context to a fraction
    n_comments_only = int(len(all_personas) * comments_only_ratio)
    for i in range(n_comments_only):
        all_personas[i]["context"] = "comments-only"

    return all_personas


def save_personas(personas: list[dict], out_path: str | Path) -> Path:
    """Write personas to JSONL file."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for p in personas:
            f.write(json.dumps(p) + "\n")
    return out


def load_personas(path: str | Path) -> list[dict]:
    """Load resolved personas from JSONL file."""
    personas = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                personas.append(json.loads(line))
    return personas
