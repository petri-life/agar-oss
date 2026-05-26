"""Agar Demo API — social simulation conversations as a service.

Endpoints:
  POST /tokens                              — mint anonymous API token
  POST /conversations                       — start conversation (runs round 1)
  GET  /conversations                       — list all conversations
  GET  /conversations/{id}                  — poll status + progress
  GET  /conversations/{id}/thread           — get conversation thread
  POST /conversations/{id}/next             — run next round (must be paused)
  POST /conversations/{id}/finish           — mark conversation as done
  POST /conversations/{id}/comment          — inject human comment
  POST /conversations/{id}/upvote/{cid}     — upvote a comment
  DELETE /conversations/{id}                — cancel conversation

Auth: X-API-Key header (auto-provisioned anonymous tokens)
Lifecycle: queued → running → paused → (next → running → paused)* → finish → done
"""

from __future__ import annotations

import logging
import os
import random
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from api import runner
from api.db import (
    init_db, create_key, get_key,
    create_conversation_if_quota, update_conversation,
    get_conversation, list_conversations,
    append_progress, get_progress,
    upvote_comment, spend_credit, refund_credit,
)
from api.sampler import assemble_population
from api.thread import load_thread, add_comment

# ── config ────────────────────────────────────────────────────

DEFAULT_CREDITS = int(os.environ.get("AGAR_DEFAULT_CREDITS", "20"))
MAX_CONCURRENT = int(os.environ.get("AGAR_MAX_CONCURRENT", "1"))
TOPIC_MIN_CHARS = 200
TOPIC_MAX_CHARS = 10000

# Comma-separated allowed origins. Defaults to "*" (open) for local/OSS use.
# Hosted deployments should set AGAR_CORS_ORIGINS to their frontend origin(s).
CORS_ORIGINS = [o.strip() for o in os.environ.get("AGAR_CORS_ORIGINS", "*").split(",") if o.strip()]

log = logging.getLogger("agar.api")


# ── lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    logging.getLogger("camel").setLevel(logging.WARNING)
    logging.getLogger("oasis").setLevel(logging.WARNING)
    logging.getLogger("social").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    init_db()
    from api.db import _conn
    with _conn() as conn:
        conn.execute(
            "UPDATE conversations SET status = 'failed', error = 'Server restarted' "
            "WHERE status IN ('queued', 'running', 'paused')"
        )
    log.info("Agar API started")
    yield
    runner.shutdown()


app = FastAPI(title="Agar Demo API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ── auth ──────────────────────────────────────────────────────

def require_api_key(request: Request) -> str:
    key = request.headers.get("X-API-Key", "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")
    row = get_key(key)
    if row is None or row["revoked"]:
        raise HTTPException(status_code=401, detail="API key revoked or invalid")
    return key


# ── request models ────────────────────────────────────────────

class CreateConversation(BaseModel):
    topic: str
    persona_mix: float = 0.5

    @field_validator("topic")
    @classmethod
    def topic_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("topic cannot be empty")
        if len(v) < TOPIC_MIN_CHARS:
            raise ValueError(f"topic too short — need at least {TOPIC_MIN_CHARS} characters")
        if len(v) > TOPIC_MAX_CHARS:
            raise ValueError(f"topic exceeds {TOPIC_MAX_CHARS} character limit")
        return v

    @field_validator("persona_mix")
    @classmethod
    def clamp_mix(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class UserComment(BaseModel):
    content: str
    parent_comment_id: int | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── label generation ──────────────────────────────────────────

_ANIMALS = ["fox", "owl", "bear", "wolf", "hawk", "lynx", "crow", "hare", "moth", "newt",
            "crab", "frog", "mole", "wren", "dove", "elk", "eel", "ant", "bee", "ram"]
_COLORS = ["coral", "amber", "slate", "jade", "rust", "plum", "sage", "teal", "onyx", "dusk",
           "mint", "sand", "iris", "fern", "zinc", "rose", "gold", "clay", "ice", "ash"]


def _generate_label() -> str:
    return f"{secrets.choice(_COLORS)}-{secrets.choice(_ANIMALS)}-{secrets.randbelow(100)}"


# ── endpoints ─────────────────────────────────────────────────

@app.post("/tokens", status_code=201)
async def mint_token() -> dict:
    key = "agar-" + secrets.token_urlsafe(24)
    label = _generate_label()
    create_key(key, label, _now(), DEFAULT_CREDITS)
    return {"token": key, "label": label, "credits": DEFAULT_CREDITS}


@app.post("/conversations", status_code=202)
async def create_conversation_endpoint(
    body: CreateConversation,
    request: Request,
) -> dict:
    api_key = require_api_key(request)

    if runner.is_at_capacity(MAX_CONCURRENT):
        raise HTTPException(status_code=429, detail=f"Max {MAX_CONCURRENT} concurrent simulation(s). Try again later.")

    conversation_id = str(uuid.uuid4())[:8]
    profiles = assemble_population(body.persona_mix, seed=random.randint(0, 99999))

    inserted = create_conversation_if_quota(
        conversation_id=conversation_id,
        session_id="pending",
        api_key=api_key,
        topic=body.topic,
        agent_count=len(profiles),
        persona_mix=body.persona_mix,
        created_at=_now(),
    )
    if not inserted:
        raise HTTPException(status_code=429, detail="Credit limit reached.")

    runner.start(conversation_id, api_key, body.topic, profiles)

    return {
        "conversation_id": conversation_id,
        "status": "queued",
        "poll": f"/conversations/{conversation_id}",
    }


@app.get("/conversations")
async def list_conversations_endpoint() -> list[dict]:
    rows = list_conversations()
    return [
        {
            "conversation_id": r["conversation_id"],
            "topic": r["topic"][:120] + "..." if len(r["topic"]) > 120 else r["topic"],
            "status": r["status"],
            "agent_count": r["agent_count"],
            "round_count": r["round_count"],
            "comment_count": r["comment_count"],
            "sim_upvotes": r["sim_upvotes"],
            "score": r["score"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/conversations/{conversation_id}")
async def get_conversation_endpoint(conversation_id: str, after: int = 0) -> dict:
    row = get_conversation(conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {
        "conversation_id": row["conversation_id"],
        "topic": row["topic"],
        "status": row["status"],
        "agent_count": row["agent_count"],
        "round_count": row["round_count"],
        "persona_mix": row["persona_mix"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "comment_count": row["comment_count"],
        "sim_upvotes": row["sim_upvotes"],
        "progress": get_progress(conversation_id, after=after),
    }


@app.get("/conversations/{conversation_id}/thread")
async def get_thread_endpoint(conversation_id: str) -> dict:
    row = get_conversation(conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if row["status"] == "queued":
        raise HTTPException(status_code=202, detail="Not started yet")
    if row["session_id"] == "pending":
        raise HTTPException(status_code=202, detail="Still initializing")

    try:
        return load_thread(row["session_id"], conversation_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session data not found")
    except sqlite3.OperationalError:
        raise HTTPException(status_code=202, detail="Simulation initializing")


@app.post("/conversations/{conversation_id}/comment")
async def add_comment_endpoint(
    conversation_id: str,
    body: UserComment,
    request: Request,
) -> dict:
    require_api_key(request)
    row = get_conversation(conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if row["session_id"] == "pending":
        raise HTTPException(status_code=409, detail="Sim not initialized yet")

    try:
        return add_comment(row["session_id"], body.content, body.parent_comment_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/conversations/{conversation_id}/upvote/{comment_id}")
async def upvote_endpoint(
    conversation_id: str, comment_id: int, request: Request,
) -> dict:
    api_key = require_api_key(request)
    row = get_conversation(conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    total = upvote_comment(conversation_id, comment_id, api_key, _now())
    return {"comment_id": comment_id, "upvotes": total}


@app.post("/conversations/{conversation_id}/next")
async def next_round_endpoint(conversation_id: str, request: Request) -> dict:
    """Run the next round. Costs 1 credit. Conversation must be paused."""
    api_key = require_api_key(request)
    if runner.is_busy(conversation_id):
        raise HTTPException(status_code=409, detail="Round already in progress")
    row = get_conversation(conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if row["round_count"] >= runner.MAX_ROUNDS:
        raise HTTPException(status_code=409, detail=f"Max {runner.MAX_ROUNDS} rounds reached")
    if not spend_credit(api_key):
        raise HTTPException(status_code=429, detail="No credits remaining")
    if not runner.next_round(conversation_id, api_key):
        refund_credit(api_key)
        raise HTTPException(status_code=409, detail="Conversation not paused")
    return {"conversation_id": conversation_id, "status": "running"}


@app.post("/conversations/{conversation_id}/finish")
async def finish_endpoint(conversation_id: str, request: Request) -> dict:
    """Mark a paused conversation as done."""
    require_api_key(request)
    if not runner.finish(conversation_id):
        raise HTTPException(status_code=409, detail="Conversation not paused")
    return {"conversation_id": conversation_id, "status": "done"}


@app.delete("/conversations/{conversation_id}")
async def kill_endpoint(conversation_id: str, request: Request) -> dict:
    api_key = require_api_key(request)
    row = get_conversation(conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if runner.cancel(conversation_id):
        update_conversation(conversation_id, status="failed", error="Cancelled", finished_at=_now())
        append_progress(conversation_id, "Cancelled by user", _now(), "error")
        refund_credit(api_key)
        return {"conversation_id": conversation_id, "status": "cancelled"}

    return {"conversation_id": conversation_id, "status": row["status"], "detail": "Not running"}


@app.get("/health")
async def health() -> dict:
    from pathlib import Path
    version_file = Path(__file__).parent.parent / "VERSION"
    version = version_file.read_text().strip() if version_file.exists() else "dev"
    return {"ok": True, "version": version, "active_sims": runner.active_count()}


# ── server ────────────────────────────────────────────────────

def run_server():
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("AGAR_RELOAD", "1") == "1"
    uvicorn.run("api.main:app", host="0.0.0.0", port=port, reload=reload)


if __name__ == "__main__":
    run_server()
