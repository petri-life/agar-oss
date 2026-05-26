"""Friction tag attribution for simulation comments.

Embeds each comment against 10 friction tag descriptions using cosine
similarity. Populates the comment_tags table in the OASIS SQLite DB.

Designed to run after each round (one step behind the sim loop) and to
scale to 10K+ agents via batched embedding — no LLM calls needed.
"""

import sqlite3
import numpy as np
from sentence_transformers import SentenceTransformer


FRICTION_TAG_DESCRIPTIONS = {
    "broken-core":          "The core feature does not work, crashes, or produces wrong results",
    "feature-noise":        "Too many features, cluttered interface, hard to find what matters",
    "onboarding-friction":  "Hard to get started, confusing setup, steep learning curve",
    "support-failure":      "No response from support, useless help docs, abandoned when stuck",
    "trust-failure":        "Privacy concerns, shady practices, feels unsafe with my data",
    "wrong-fit":            "Product does not match my use case or workflow",
    "value-unclear":        "Cannot tell what this does or why I would pay for it",
    "switching-cost":       "Too hard to migrate data in or out, locked into the platform",
    "platform-dependency":  "Breaks when OS or third-party service changes, fragile integrations",
    "habit-gap":            "Good in theory but I keep forgetting to use it, does not stick",
}

TAGS = list(FRICTION_TAG_DESCRIPTIONS.keys())


class Tagger:
    """Lazy-loading friction tagger using sentence embeddings."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model: SentenceTransformer | None = None
        self._tag_embeddings: np.ndarray | None = None

    def _ensure_loaded(self) -> tuple[SentenceTransformer, np.ndarray]:
        if self._model is None:
            self._model = SentenceTransformer(self._model_name)
            self._tag_embeddings = self._model.encode(
                list(FRICTION_TAG_DESCRIPTIONS.values()),
                normalize_embeddings=True,
            )
        return self._model, self._tag_embeddings

    def tag_new_comments(self, db: str, batch_size: int = 50) -> int:
        """Embed and tag all comments not yet in comment_tags.

        Returns:
            Number of comments tagged.
        """
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)

        rows = conn.execute("""
            SELECT c.comment_id, c.content
            FROM comment c
            LEFT JOIN comment_tags ct ON ct.comment_id = c.comment_id
            WHERE ct.comment_id IS NULL
        """).fetchall()

        if not rows:
            conn.close()
            return 0

        model, tag_embeddings = self._ensure_loaded()
        total = 0

        for batch_start in range(0, len(rows), batch_size):
            batch = rows[batch_start: batch_start + batch_size]
            texts = [r["content"] for r in batch]
            embeddings = model.encode(texts, normalize_embeddings=True)
            scores = embeddings @ tag_embeddings.T

            conn.executemany(
                "INSERT OR IGNORE INTO comment_tags (comment_id, tag, score) VALUES (?, ?, ?)",
                [
                    (batch[i]["comment_id"], TAGS[j], float(scores[i, j]))
                    for i in range(len(batch))
                    for j in range(len(TAGS))
                ],
            )
            conn.commit()
            total += len(batch)

        conn.close()
        return total


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create comment_tags table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comment_tags (
            comment_id  INTEGER NOT NULL,
            tag         TEXT NOT NULL,
            score       REAL NOT NULL,
            PRIMARY KEY (comment_id, tag)
        )
    """)
    conn.commit()


def dominant_tag(db: str, comment_id: int) -> str | None:
    """Return the highest-scoring friction tag for a comment."""
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT tag FROM comment_tags WHERE comment_id = ? ORDER BY score DESC LIMIT 1",
        (comment_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None
