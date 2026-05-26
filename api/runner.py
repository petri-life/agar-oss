"""Background simulation runner — one round at a time.

Each POST /conversations creates the session and runs round 1.
Each POST /conversations/{id}/next runs the next round.
The thread exits after each round — no long-lived background threads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone

from api.db import (
    update_conversation, append_progress, refund_credit,
)

DEFAULT_MODEL = os.environ.get("AGAR_MODEL", "haiku")
DEFAULT_TIMEOUT = float(os.environ.get("AGAR_TIMEOUT", "60"))
DEFAULT_ACTIVATION = float(os.environ.get("AGAR_ACTIVATION", "0"))
MAX_ROUNDS = int(os.environ.get("AGAR_MAX_ROUNDS", "10"))
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

log = logging.getLogger("agar.runner")

_running: dict[str, threading.Thread] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_stats(conversation_id: str, session_id: str) -> None:
    import sqlite3
    from sim.session import Session
    try:
        session = Session.load(session_id)
        conn = sqlite3.connect(session.live_db)
        comment_count = conn.execute("SELECT COUNT(*) FROM comment").fetchone()[0]
        sim_upvotes = conn.execute("SELECT COALESCE(SUM(num_likes), 0) FROM comment").fetchone()[0]
        conn.close()
        update_conversation(conversation_id, comment_count=comment_count, sim_upvotes=sim_upvotes)
    except Exception:
        pass


def _run_round_sync(
    conversation_id: str,
    api_key: str,
    topic: str,
    profiles: list[dict],
    is_first: bool,
) -> None:
    """Run a single round. Creates the session on first call."""
    from sim.session import Session

    loop = asyncio.new_event_loop()
    session = None
    try:
        if is_first:
            update_conversation(conversation_id, status="running", started_at=_now())
            append_progress(conversation_id, f"Assembled {len(profiles)} agents", _now(), "init")
            log.info("Conversation %s: creating session with %d agents", conversation_id, len(profiles))

            session = Session.create_from_profiles(
                profiles=profiles,
                brief=topic,
                model=DEFAULT_MODEL,
                timeout=DEFAULT_TIMEOUT,
                activation=DEFAULT_ACTIVATION,
                analyze=False,
            )

            if OPENROUTER_API_KEY:
                from sim.openrouter_model import OpenRouterModel
                session._model_backend = OpenRouterModel(timeout=DEFAULT_TIMEOUT)

            update_conversation(conversation_id, session_id=session.session_id)
            append_progress(conversation_id, "Round 1 starting", _now(), "running")
        else:
            from api.db import get_conversation
            row = get_conversation(conversation_id)
            session = Session.load(row["session_id"])
            if OPENROUTER_API_KEY:
                from sim.openrouter_model import OpenRouterModel
                session._model_backend = OpenRouterModel(timeout=DEFAULT_TIMEOUT)
            update_conversation(conversation_id, status="running")
            round_num = row["round_count"] + 1
            append_progress(conversation_id, f"Round {round_num} starting", _now(), "running")

        def on_round(round_num, highlight):
            update_conversation(conversation_id, round_count=round_num)
            append_progress(conversation_id, f"Round {round_num}: {highlight}", _now(), "round")
            log.info("Conversation %s: round %d — %s", conversation_id, round_num, highlight)

        loop.run_until_complete(
            session.run(rounds=1, on_round=on_round)
        )

        _snapshot_stats(conversation_id, session.session_id)
        update_conversation(conversation_id, status="paused")
        log.info("Conversation %s: round complete, paused", conversation_id)

    except Exception as e:
        log.exception("Conversation %s failed", conversation_id)
        update_conversation(conversation_id, status="failed", error=str(e), finished_at=_now())
        append_progress(conversation_id, f"Failed: {e}", _now(), "error")
        refund_credit(api_key)  # refund the round credit on failure
    finally:
        if session:
            try:
                loop.run_until_complete(session.close())
            except Exception:
                pass
        loop.close()
        _running.pop(conversation_id, None)


def start(conversation_id: str, api_key: str, topic: str, profiles: list[dict]) -> None:
    """Create session and run round 1 in a background thread."""
    t = threading.Thread(
        target=_run_round_sync,
        args=(conversation_id, api_key, topic, profiles, True),
        daemon=True,
    )
    _running[conversation_id] = t
    t.start()


def next_round(conversation_id: str, api_key: str) -> bool:
    """Run the next round. Returns False if already running or at max rounds."""
    if conversation_id in _running and _running[conversation_id].is_alive():
        return False

    from api.db import get_conversation
    row = get_conversation(conversation_id)
    if not row or row["status"] not in ("paused",):
        return False
    if row["round_count"] >= MAX_ROUNDS:
        return False

    t = threading.Thread(
        target=_run_round_sync,
        args=(conversation_id, api_key, "", [], False),
        daemon=True,
    )
    _running[conversation_id] = t
    t.start()
    return True


def finish(conversation_id: str) -> bool:
    """Mark a paused conversation as done."""
    from api.db import get_conversation
    row = get_conversation(conversation_id)
    if not row or row["status"] != "paused":
        return False
    _snapshot_stats(conversation_id, row["session_id"])
    update_conversation(conversation_id, status="done", finished_at=_now())
    append_progress(conversation_id, "Conversation finished", _now(), "done")
    return True


def cancel(conversation_id: str) -> bool:
    """Cancel a running conversation."""
    t = _running.get(conversation_id)
    if t and t.is_alive():
        # Can't cleanly kill a thread — mark as failed, it'll finish eventually
        update_conversation(conversation_id, status="failed", error="Cancelled", finished_at=_now())
        append_progress(conversation_id, "Cancelled", _now(), "error")
        _running.pop(conversation_id, None)
        return True
    return False


def is_busy(conversation_id: str) -> bool:
    t = _running.get(conversation_id)
    return t is not None and t.is_alive()


def active_count() -> int:
    return sum(1 for t in _running.values() if t.is_alive())


def is_at_capacity(max_concurrent: int) -> bool:
    return active_count() >= max_concurrent


def shutdown() -> None:
    pass  # daemon threads die with the process
