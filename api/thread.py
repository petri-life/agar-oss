"""Thread enrichment — load OASIS thread data and enrich with persona info + upvotes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from sim.html_report import _load_thread_data
from sim.session import Session
from api.db import get_comment_upvotes

SHOW_PERSONAS = os.environ.get("AGAR_SHOW_PERSONAS", "0") == "1"

_ANIMALS = ["fox", "owl", "bear", "wolf", "hawk", "lynx", "crow", "hare", "moth", "newt",
            "crab", "frog", "mole", "wren", "dove", "elk", "eel", "ant", "bee", "ram"]
_COLORS = ["coral", "amber", "slate", "jade", "rust", "plum", "sage", "teal", "onyx", "dusk",
           "mint", "sand", "iris", "fern", "zinc", "rose", "gold", "clay", "ice", "ash"]


def load_thread(session_id: str, conversation_id: str) -> dict:
    """Load thread from OASIS DB, enrich with persona names and upvotes.

    Args:
        session_id: Agar session ID (sim_YYYYMMDD_HHMMSS).
        conversation_id: API conversation ID.

    Returns:
        Thread dict with posts, comments, users.

    Raises:
        FileNotFoundError: Session not found.
        sqlite3.OperationalError: DB not ready yet.
    """
    session = Session.load(session_id)
    thread_data = _load_thread_data(session.live_db)

    _enrich_users(thread_data, session)
    _enrich_upvotes(thread_data, conversation_id)
    _rename_scores(thread_data)

    return thread_data


def add_comment(session_id: str, content: str, parent_comment_id: int | None = None) -> dict:
    """Insert a human comment into the OASIS DB.

    Returns:
        {"comment_id": int, "user_id": int}
    """
    from datetime import datetime

    session = Session.load(session_id)
    conn = sqlite3.connect(session.live_db)
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

        product_user = conn.execute(
            "SELECT user_id FROM user WHERE name = 'product_team'"
        ).fetchone()
        if not product_user:
            raise ValueError("Product team user not found in OASIS DB")
        user_id = product_user[0]

        post = conn.execute("SELECT post_id FROM post LIMIT 1").fetchone()
        if not post:
            raise ValueError("No post in conversation yet")
        post_id = post[0]

        if parent_comment_id:
            parent = conn.execute(
                "SELECT comment_id FROM comment WHERE comment_id = ?",
                (parent_comment_id,),
            ).fetchone()
            if not parent:
                raise ValueError(f"Parent comment {parent_comment_id} not found")
            conn.execute(
                "INSERT INTO comment (post_id, user_id, content, created_at, "
                "parent_comment_id, num_likes, num_dislikes) VALUES (?, ?, ?, ?, ?, 0, 0)",
                (post_id, user_id, content, now, parent_comment_id),
            )
        else:
            conn.execute(
                "INSERT INTO comment (post_id, user_id, content, created_at, "
                "num_likes, num_dislikes) VALUES (?, ?, ?, ?, 0, 0)",
                (post_id, user_id, content, now),
            )

        comment_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return {"comment_id": comment_id, "user_id": user_id}
    finally:
        conn.close()


def _enrich_users(thread_data: dict, session: Session) -> None:
    """Add persona names to users from profiles_full.jsonl."""
    full_path = session.session_dir / "profiles_full.jsonl"
    if not full_path.exists():
        return

    profiles = []
    with open(full_path) as f:
        for line in f:
            if line.strip():
                profiles.append(json.loads(line))

    for i, p in enumerate(profiles):
        if i not in thread_data["users"]:
            continue
        user = thread_data["users"][i]
        slug = p.get("uid") or p.get("persona_id", f"user_{i}")

        if SHOW_PERSONAS:
            from sim.agent_factory import _build_persona
            user["name"] = slug
            user["role"] = p.get("role") or p.get("tone") or p.get("dominant", "")
            user["context"] = p.get("context", "full")
            user["bio"] = _build_persona(p)
        else:
            h = hashlib.md5(slug.encode()).digest()
            user["name"] = f"{_COLORS[h[0] % len(_COLORS)]}-{_ANIMALS[h[1] % len(_ANIMALS)]}"
            user["bio"] = ""


def _enrich_upvotes(thread_data: dict, conversation_id: str) -> None:
    """Add human upvote counts to comments."""
    upvotes = get_comment_upvotes(conversation_id)
    for comment in thread_data["comments"]:
        comment["upvotes"] = upvotes.get(comment["comment_id"], 0)


def _rename_scores(thread_data: dict) -> None:
    """Rename OASIS 'score' to 'sim_score' to distinguish from human upvotes."""
    for comment in thread_data["comments"]:
        comment["sim_score"] = comment.pop("score", 0)
    for post in thread_data["posts"]:
        post["sim_score"] = post.pop("score", 0)
