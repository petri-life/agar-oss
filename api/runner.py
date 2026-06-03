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
    update_conversation, append_progress, decrement_cents, record_round_cost,
)

DEFAULT_MODEL = os.environ.get("AGAR_MODEL", "haiku")
DEFAULT_TIMEOUT = float(os.environ.get("AGAR_TIMEOUT", "60"))
DEFAULT_ACTIVATION = float(os.environ.get("AGAR_ACTIVATION", "0"))
MAX_ROUNDS = int(os.environ.get("AGAR_MAX_ROUNDS", "10"))
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")


def _usd_to_cents(usd: float) -> int:
    """Round USD up to whole cents — never under-bill a fractional cent."""
    import math
    return max(0, math.ceil(usd * 100))

log = logging.getLogger("agar.runner")

_running: dict[str, threading.Thread] = {}

# Per-sim cooperative-cancel signal. Set by cancel(); the OpenRouter backend's
# call paths check it before each LLM request and raise asyncio.CancelledError
# if set. asyncio.gather then aborts the round, the runner's exception handler
# bills the partial spend, and the sim transitions to failed cleanly.
#
# Why an Event instead of just polling the DB row's status?
#   - threading.Event is cheap to read, no DB hit per LLM call.
#   - Cancel originates in an HTTP handler thread; Event is the safe primitive
#     for cross-thread signal without locks on every check.
_cancellation_events: dict[str, threading.Event] = {}


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
    """Run a single round. Creates the session on first call.

    The OpenRouter model used is sourced from the conversation row's model_id
    (set by POST /conversations and locked for the sim). All rounds of a sim
    use the same model so cost is predictable from /me/conversations.
    """
    from sim.session import Session
    from api.db import get_conversation
    from api.models import get_tier

    loop = asyncio.new_event_loop()
    session = None
    # Cooperative cancel: one Event per running sim. cancel() sets it; the
    # OpenRouter backend's per-call check raises CancelledError, gather()
    # aborts, the round's spend is reconciled in the except path below.
    cancel_event = threading.Event()
    _cancellation_events[conversation_id] = cancel_event
    try:
        # Resolve the tier once; the runner uses the same OpenRouter model id
        # for both round-1 (is_first) and /next paths.
        row = get_conversation(conversation_id)
        tier = get_tier(row["model_id"] if row else None)

        if is_first:
            update_conversation(conversation_id, status="running", started_at=_now())
            append_progress(conversation_id, f"Assembled {len(profiles)} agents", _now(), "init")
            log.info("Conversation %s: creating session with %d agents on tier=%s (%s)",
                     conversation_id, len(profiles), tier.name, tier.model)

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
                backend = OpenRouterModel(model=tier.model, timeout=DEFAULT_TIMEOUT)
                backend._cancel_event = cancel_event
                session._model_backend = backend

            update_conversation(conversation_id, session_id=session.session_id)
            append_progress(conversation_id, "Round 1 starting", _now(), "running")
        else:
            session = Session.load(row["session_id"])
            if OPENROUTER_API_KEY:
                from sim.openrouter_model import OpenRouterModel
                backend = OpenRouterModel(model=tier.model, timeout=DEFAULT_TIMEOUT)
                backend._cancel_event = cancel_event
                session._model_backend = backend
            update_conversation(conversation_id, status="running")
            round_num = row["round_count"] + 1
            append_progress(conversation_id, f"Round {round_num} starting", _now(), "running")

        def on_round(round_num, highlight):
            update_conversation(conversation_id, round_count=round_num)
            append_progress(conversation_id, f"Round {round_num}: {highlight}", _now(), "round")
            log.info("Conversation %s: round %d — %s", conversation_id, round_num, highlight)

        # Reset cost accumulator so we measure only this round's spend.
        backend = getattr(session, "_model_backend", None)
        meters_cost = backend is not None and hasattr(backend, "reset_cost")
        if meters_cost:
            backend.reset_cost()

        loop.run_until_complete(
            session.run(rounds=1, on_round=on_round)
        )

        # Reconcile: bill the real cost this round actually incurred. Deducting
        # after the round means a round that started always settles its bill —
        # we never strand a half-charged round. The Claude-CLI backend has no
        # cost data (meters_cost False), so it runs unmetered (free local tier).
        if meters_cost:
            spent_usd = backend.reset_cost()
            spent_cents = _usd_to_cents(spent_usd)
            new_balance = decrement_cents(api_key, spent_cents)
            # round_count is the just-completed round (on_round set it above).
            # Read it back so we record the cost against the right round_num
            # even when the runner picks up an existing session for /next.
            from api.db import get_conversation
            current_round = get_conversation(conversation_id)["round_count"]
            record_round_cost(conversation_id, current_round, spent_cents, _now())
            update_conversation(conversation_id, last_round_cost_cents=spent_cents)
            append_progress(
                conversation_id,
                f"Round cost: {spent_cents}¢ — balance {new_balance}¢",
                _now(), "cost",
            )
            log.info("Conversation %s: billed %d¢ (USD %.4f), balance %d¢",
                     conversation_id, spent_cents, spent_usd, new_balance)
            # L3 output sanitization: count how many agent replies got
            # replaced this round. Non-zero means the topic tried to exfiltrate
            # persona data; the defense caught it.
            if hasattr(backend, "reset_sanitized_count"):
                n_sanitized = backend.reset_sanitized_count()
                if n_sanitized > 0:
                    append_progress(
                        conversation_id,
                        f"Sanitized {n_sanitized} agent comment(s) for leaked persona content",
                        _now(), "security",
                    )
                    log.warning("Conversation %s: sanitized %d comments (persona-leak)",
                                conversation_id, n_sanitized)

        _snapshot_stats(conversation_id, session.session_id)
        update_conversation(conversation_id, status="paused")
        log.info("Conversation %s: round complete, paused", conversation_id)

    except (Exception, asyncio.CancelledError) as e:
        # CancelledError is a BaseException in Python 3.8+, NOT caught by
        # bare `except Exception` — we list it explicitly so cooperative
        # cancel (via _cancellation_events) lands in this reconcile path
        # instead of leaking past the runner and leaving cost unbilled.
        is_cancel = isinstance(e, asyncio.CancelledError) or cancel_event.is_set()
        if is_cancel:
            log.info("Conversation %s cancelled by user", conversation_id)
            update_conversation(conversation_id, status="failed", error="Cancelled by user", finished_at=_now())
            append_progress(conversation_id, "Cancelled by user", _now(), "error")
        else:
            log.exception("Conversation %s failed", conversation_id)
            update_conversation(conversation_id, status="failed", error=str(e), finished_at=_now())
            append_progress(conversation_id, f"Failed: {e}", _now(), "error")
        # Estimate-then-reconcile: a failed/cancelled round still bills whatever
        # real cost it incurred before bailing (calls already cost money).
        # Capture the partial spend rather than refunding or charging a flat
        # round. Sentinel round_num=0 keeps these out of the real-round table.
        backend = getattr(session, "_model_backend", None)
        if backend is not None and hasattr(backend, "reset_cost"):
            spent_cents = _usd_to_cents(backend.reset_cost())
            if spent_cents:
                decrement_cents(api_key, spent_cents)
                from api.db import get_round_costs
                prior = next(
                    (r["cost_cents"] for r in get_round_costs(conversation_id)
                     if r["round_num"] == 0),
                    0,
                )
                record_round_cost(conversation_id, 0, prior + spent_cents, _now())
                log.info("Conversation %s: billed %d¢ for partial round (cancel=%s)",
                         conversation_id, spent_cents, is_cancel)
    finally:
        if session:
            try:
                loop.run_until_complete(session.close())
            except Exception:
                pass
        loop.close()
        _running.pop(conversation_id, None)
        _cancellation_events.pop(conversation_id, None)


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
    """Cancel a running conversation cooperatively.

    Sets a threading.Event the OpenRouter backend checks before each LLM
    request. The current in-flight HTTP request (if any) finishes — we
    eat that one call's spend — but the next 30 agents in the round's
    asyncio.gather get CancelledError and the round bails. The runner's
    exception handler then reconciles the partial spend and marks the
    sim failed.

    Returns True if a cancel signal was sent. The thread is NOT joined
    here; it'll complete its in-flight call (typically <30s) and clean
    up via its own finally. DB row status is updated by the runner once
    the round actually unwinds — NOT here. Callers needing immediate UI
    state should poll /conversations/{id} after this returns.
    """
    t = _running.get(conversation_id)
    if t and t.is_alive():
        ev = _cancellation_events.get(conversation_id)
        if ev is not None:
            ev.set()
        # NOTE: we used to mark the row failed RIGHT HERE, before the runner
        # had actually wound down. That created the bug where the round
        # continued spending money on a sim already marked failed — and
        # later wrote "Round 1: done" on top of "Cancelled by user". Now
        # the runner owns the state transition.
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
