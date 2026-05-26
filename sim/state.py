"""Tag/revert state management via SQLite DB snapshots.

OASIS persists all simulation state to a single SQLite file.
Tagging = copy the DB. Reverting = restore from copy.

Directory layout:
    data/simulations/{sim_id}/
    ├── state.db                 # live simulation DB
    ├── tags/
    │   ├── baseline.db          # tagged snapshot
    │   ├── change-1.db
    │   └── change-2.db
    └── reports/
        ├── baseline.md
        └── change-1.md
"""

import shutil
from pathlib import Path


def sim_dir(base: str | Path, sim_id: str) -> Path:
    """Return the simulation directory, creating it if needed."""
    d = Path(base) / sim_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path(base: str | Path, sim_id: str) -> Path:
    """Return the live DB path for a simulation."""
    return sim_dir(base, sim_id) / "state.db"


def tag(base: str | Path, sim_id: str, tag_name: str) -> Path:
    """Snapshot the live DB to a named tag.

    Returns the path to the tagged snapshot.

    Raises:
        FileNotFoundError: If the live DB doesn't exist yet.
    """
    live = db_path(base, sim_id)
    if not live.exists():
        raise FileNotFoundError(f"No live DB at {live}")

    tags_dir = sim_dir(base, sim_id) / "tags"
    tags_dir.mkdir(exist_ok=True)

    dest = tags_dir / f"{tag_name}.db"
    shutil.copy2(live, dest)
    return dest


def revert(base: str | Path, sim_id: str, tag_name: str) -> Path:
    """Restore the live DB from a named tag.

    Returns the live DB path.

    Raises:
        FileNotFoundError: If the tag doesn't exist.
    """
    source = sim_dir(base, sim_id) / "tags" / f"{tag_name}.db"
    if not source.exists():
        raise FileNotFoundError(f"No tag '{tag_name}' for sim {sim_id}")

    live = db_path(base, sim_id)
    shutil.copy2(source, live)
    return live


def list_tags(base: str | Path, sim_id: str) -> list[str]:
    """List all available tags for a simulation."""
    tags_dir = sim_dir(base, sim_id) / "tags"
    if not tags_dir.exists():
        return []
    return sorted(p.stem for p in tags_dir.glob("*.db"))


def report_path(base: str | Path, sim_id: str, tag_name: str) -> Path:
    """Return the report file path for a tag, creating reports/ dir."""
    reports_dir = sim_dir(base, sim_id) / "reports"
    reports_dir.mkdir(exist_ok=True)
    return reports_dir / f"{tag_name}.md"
