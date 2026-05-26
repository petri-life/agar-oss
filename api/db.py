"""SQLite persistence for Agar API. Single file, no ORM."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "agar_api.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key             TEXT PRIMARY KEY,
                label           TEXT NOT NULL DEFAULT 'anon',
                created_at      TEXT NOT NULL,
                revoked         INTEGER NOT NULL DEFAULT 0,
                credits_remaining INTEGER NOT NULL DEFAULT 3
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                api_key         TEXT NOT NULL,
                topic           TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'queued',
                agent_count     INTEGER NOT NULL,
                round_count     INTEGER NOT NULL DEFAULT 0,
                persona_mix     REAL NOT NULL DEFAULT 0.5,
                created_at      TEXT NOT NULL,
                started_at      TEXT,
                finished_at     TEXT,
                error           TEXT,
                comment_count   INTEGER NOT NULL DEFAULT 0,
                sim_upvotes     INTEGER NOT NULL DEFAULT 0
            )
        """)
        # migrations for existing databases
        for col in ("comment_count INTEGER NOT NULL DEFAULT 0", "sim_upvotes INTEGER NOT NULL DEFAULT 0"):
            try:
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_api_key ON conversations(api_key)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS progress_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                message         TEXT NOT NULL,
                stage           TEXT NOT NULL DEFAULT 'info',
                created_at      TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_progress_conv ON progress_log(conversation_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comment_upvotes (
                conversation_id TEXT NOT NULL,
                comment_id      INTEGER NOT NULL,
                api_key         TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                PRIMARY KEY (conversation_id, comment_id, api_key)
            )
        """)


# ── API keys ─────────────────────────────────────────────────

def create_key(key: str, label: str, created_at: str, credits: int = 3) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO api_keys (key, label, created_at, credits_remaining) VALUES (?, ?, ?, ?)",
            (key, label, created_at, credits),
        )


def get_key(key: str) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute("SELECT * FROM api_keys WHERE key = ?", (key,)).fetchone()


def list_keys() -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()


def revoke_key(key: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE api_keys SET revoked = 1 WHERE key = ?", (key,))


def topup_key(key: str, credits: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE api_keys SET credits_remaining = credits_remaining + ? WHERE key = ?",
            (credits, key),
        )


def spend_credit(api_key: str) -> bool:
    """Deduct 1 credit atomically. Returns False if none remaining."""
    with _conn() as conn:
        conn.execute("BEGIN EXCLUSIVE")
        row = conn.execute(
            "SELECT credits_remaining FROM api_keys WHERE key = ? AND revoked = 0",
            (api_key,),
        ).fetchone()
        if not row or row["credits_remaining"] <= 0:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE api_keys SET credits_remaining = credits_remaining - 1 WHERE key = ?",
            (api_key,),
        )
        return True


def refund_credit(api_key: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE api_keys SET credits_remaining = credits_remaining + 1 WHERE key = ?",
            (api_key,),
        )


# ── Conversations ────────────────────────────────────────────

def create_conversation_if_quota(
    conversation_id: str,
    session_id: str,
    api_key: str,
    topic: str,
    agent_count: int,
    persona_mix: float,
    created_at: str,
) -> bool:
    """Decrement credits and insert conversation atomically.
    Returns True if inserted, False if no credits remaining."""
    with _conn() as conn:
        conn.execute("BEGIN EXCLUSIVE")
        row = conn.execute(
            "SELECT credits_remaining FROM api_keys WHERE key = ? AND revoked = 0",
            (api_key,),
        ).fetchone()
        if not row or row["credits_remaining"] <= 0:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE api_keys SET credits_remaining = credits_remaining - 1 WHERE key = ?",
            (api_key,),
        )
        conn.execute(
            """INSERT INTO conversations
               (conversation_id, session_id, api_key, topic, status, agent_count, persona_mix, created_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
            (conversation_id, session_id, api_key, topic, agent_count, persona_mix, created_at),
        )
        return True


def update_conversation(conversation_id: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [conversation_id]
    with _conn() as conn:
        conn.execute(f"UPDATE conversations SET {cols} WHERE conversation_id = ?", vals)


def get_conversation(conversation_id: str) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()


def list_conversations() -> list[sqlite3.Row]:
    """All conversations, newest first, with upvote score."""
    with _conn() as conn:
        return conn.execute("""
            SELECT c.conversation_id, c.session_id, c.topic, c.status, c.agent_count,
                   c.round_count, c.persona_mix, c.created_at, c.finished_at,
                   c.comment_count, c.sim_upvotes,
                   COALESCE(u.score, 0) as score
            FROM conversations c
            LEFT JOIN (
                SELECT conversation_id, COUNT(*) as score
                FROM comment_upvotes GROUP BY conversation_id
            ) u ON c.conversation_id = u.conversation_id
            ORDER BY c.created_at DESC
        """).fetchall()


# ── Progress ─────────────────────────────────────────────────

def append_progress(conversation_id: str, message: str, created_at: str, stage: str = "info") -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO progress_log (conversation_id, message, stage, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, message, stage, created_at),
        )
        return cur.lastrowid


def get_progress(conversation_id: str, after: int = 0) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, message, stage, created_at FROM progress_log "
            "WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, after),
        ).fetchall()
        return [{"id": r["id"], "message": r["message"], "stage": r["stage"], "ts": r["created_at"]} for r in rows]


# ── Upvotes ──────────────────────────────────────────────────

def upvote_comment(conversation_id: str, comment_id: int, api_key: str, created_at: str) -> int:
    """Upvote a comment. Idempotent per token+comment. Returns total upvotes for that comment."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO comment_upvotes (conversation_id, comment_id, api_key, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, comment_id, api_key, created_at),
        )
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM comment_upvotes WHERE conversation_id = ? AND comment_id = ?",
            (conversation_id, comment_id),
        ).fetchone()
        return row["cnt"]


def get_comment_upvotes(conversation_id: str) -> dict[int, int]:
    """Returns {comment_id: upvote_count} for all upvoted comments in a conversation."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT comment_id, COUNT(*) as cnt FROM comment_upvotes "
            "WHERE conversation_id = ? GROUP BY comment_id",
            (conversation_id,),
        ).fetchall()
        return {r["comment_id"]: r["cnt"] for r in rows}
