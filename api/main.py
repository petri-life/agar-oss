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
    get_conversation, list_conversations, list_user_conversations,
    append_progress, get_progress,
    upvote_comment, get_balance, has_balance, topup_key,
    get_round_costs,
)
from api.models import get_tier
from api.sampler import assemble_population
from api.security import (
    InputRejected, check_topic_pattern, moderate_topic_with_llm,
)
from api.thread import load_thread, add_comment

# ── config ────────────────────────────────────────────────────

# Balances are in cents. A new token's starting balance (default 0 — hosted
# tops up via Stripe; local dev can set a free allowance).
DEFAULT_CREDIT_CENTS = int(os.environ.get("AGAR_DEFAULT_CREDIT_CENTS", "0"))
# Worst-case per-round cost used only to GATE a round start. Actual cost is
# reconciled after the round from real usage.cost. 0 = unmetered gate.
ROUND_ESTIMATE_CENTS = int(os.environ.get("AGAR_ROUND_ESTIMATE_CENTS", "10"))
# Whether this deployment meters cost at all (only the OpenRouter backend
# returns cost data; the Claude-CLI free tier does not).
METERED = bool(os.environ.get("OPENROUTER_API_KEY", ""))
# Shared secret guarding token minting and credit top-ups. When set, callers
# must send X-Mint-Secret. Unset (OSS default) = open, free local use.
MINT_SECRET = os.environ.get("AGAR_MINT_SECRET", "")
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


def require_mint_secret(request: Request) -> None:
    """Gate privileged minting/top-up endpoints.

    When AGAR_MINT_SECRET is set (hosted), callers must send a matching
    X-Mint-Secret header. When unset (OSS default), the gate is open so local
    deployments mint freely. Uses a constant-time compare to avoid leaking the
    secret via timing.
    """
    if not MINT_SECRET:
        return
    provided = request.headers.get("X-Mint-Secret", "")
    if not secrets.compare_digest(provided, MINT_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Mint-Secret")


# ── request models ────────────────────────────────────────────

class CreateConversation(BaseModel):
    topic: str
    persona_mix: float = 0.5
    # Tier name: "flash" (default), "pro", or "sonnet". Unknown values fall
    # back to the configured default in get_tier(). Locked at sim creation.
    model: str | None = None

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
async def mint_token(request: Request) -> dict:
    require_mint_secret(request)
    key = "agar-" + secrets.token_urlsafe(24)
    label = _generate_label()
    create_key(key, label, _now(), DEFAULT_CREDIT_CENTS)
    return {"token": key, "label": label, "credit_cents": DEFAULT_CREDIT_CENTS}


class AddCredits(BaseModel):
    token: str
    cents: int

    @field_validator("cents")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("cents must be positive")
        return v


@app.post("/credits/add")
async def add_credits_endpoint(body: AddCredits, request: Request) -> dict:
    """Top up a token's balance (cents). Gated by X-Mint-Secret.

    Called by the hosted payment webhook after a successful charge. The webhook
    — not the browser — holds the mint secret, so users can't self-credit.
    """
    require_mint_secret(request)
    row = get_key(body.token)
    if row is None or row["revoked"]:
        raise HTTPException(status_code=404, detail="Token not found")
    topup_key(body.token, body.cents)
    return {"token": body.token, "balance_cents": get_balance(body.token)}


@app.get("/tiers")
async def list_tiers_endpoint() -> dict:
    """Public list of model tiers + their per-round estimate.

    The FE reads this to build the Composer model picker. Estimate is the
    server-side balance gate, so it doubles as a UX hint ('Sonnet round
    needs ~170¢').
    """
    from api.models import TIERS, DEFAULT_TIER
    return {
        "default": DEFAULT_TIER,
        "tiers": [
            {"name": t.name, "model": t.model, "estimate_cents": t.estimate_cents}
            for t in TIERS.values()
        ],
    }


@app.get("/balance")
async def get_balance_endpoint(request: Request) -> dict:
    """Read the current balance for the calling token.

    Authenticated by X-API-Key (token holder reads their own balance — not a
    privileged op). Frontends need this on page load: balance is otherwise
    only echoed by mutations, so without a probe the UI cannot show credits
    until the user spends some.
    """
    api_key = require_api_key(request)
    return {"balance_cents": get_balance(api_key)}


@app.post("/conversations", status_code=202)
async def create_conversation_endpoint(
    body: CreateConversation,
    request: Request,
) -> dict:
    api_key = require_api_key(request)

    if runner.is_at_capacity(MAX_CONCURRENT):
        raise HTTPException(status_code=429, detail=f"Max {MAX_CONCURRENT} concurrent simulation(s). Try again later.")

    # ── L1 + L2: prompt-injection defenses ─────────────────────
    # See api/security.py. Layered. Both run BEFORE we spend any sim cost.
    try:
        check_topic_pattern(body.topic)
    except InputRejected as e:
        log.info("Rejected topic (pattern=%s) from %s", e.pattern_label, api_key[:12])
        raise HTTPException(
            status_code=400,
            detail=f"Topic rejected: matches a known prompt-injection pattern ({e.pattern_label}). "
                   "Rephrase as a normal product idea or discussion topic.",
        )
    mod = moderate_topic_with_llm(body.topic)
    if mod.flagged:
        log.info("Rejected topic (moderation=%s) from %s", mod.reason, api_key[:12])
        raise HTTPException(
            status_code=400,
            detail="Topic rejected by content review. Submit a normal product idea "
                   "or discussion topic.",
        )
    elif mod.reason.startswith("moderation_error"):
        # Fail-open path: record that we let this through despite the moderator
        # being unavailable. Surfaces ops issues; doesn't block submissions.
        log.warning("Moderation bypassed for %s: %s", api_key[:12], mod.reason)

    # Resolve the tier from the request, locking it for the lifetime of this
    # sim. The estimate is per-tier so Sonnet rounds get gated at ~$1 while
    # Flash rounds gate at ~10¢ — without this, a Sonnet round could start
    # with a 10¢ balance and burn $1 of real OpenRouter credit on us.
    tier = get_tier(body.model)
    estimate = tier.estimate_cents

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
        min_cents=estimate if METERED else 0,
        model_id=tier.name,
    )
    if not inserted:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient balance. {tier.name} round needs ~{estimate}¢; "
                   f"you have {get_balance(api_key)}¢.",
        )

    runner.start(conversation_id, api_key, body.topic, profiles)

    return {
        "conversation_id": conversation_id,
        "status": "queued",
        "poll": f"/conversations/{conversation_id}",
        "balance_cents": get_balance(api_key),
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
            "model_id": r["model_id"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/me/conversations")
async def list_my_conversations_endpoint(request: Request) -> list[dict]:
    """The caller's own conversations with cost history. Authenticated.

    Distinct from public GET /conversations which omits cost columns entirely.
    Each entry includes a `rounds` array with one entry per recorded round
    (round_num, cost_cents, recorded_at). round_num=0 is a partial-failure
    bucket; positive ints are real completed rounds.
    """
    api_key = require_api_key(request)
    rows = list_user_conversations(api_key)
    out: list[dict] = []
    for r in rows:
        cid = r["conversation_id"]
        rounds = [
            {
                "round_num": rc["round_num"],
                "cost_cents": rc["cost_cents"],
                "recorded_at": rc["recorded_at"],
            }
            for rc in get_round_costs(cid)
        ]
        out.append({
            "conversation_id": cid,
            "topic": r["topic"][:120] + "..." if len(r["topic"]) > 120 else r["topic"],
            "status": r["status"],
            "agent_count": r["agent_count"],
            "round_count": r["round_count"],
            "comment_count": r["comment_count"],
            "sim_upvotes": r["sim_upvotes"],
            "score": r["score"],
            "created_at": r["created_at"],
            "finished_at": r["finished_at"],
            "total_cost_cents": r["total_cost_cents"],
            "last_round_cost_cents": r["last_round_cost_cents"],
            "model_id": r["model_id"],
            "rounds": rounds,
        })
    return out


@app.get("/conversations/{conversation_id}")
async def get_conversation_endpoint(conversation_id: str, request: Request, after: int = 0) -> dict:
    row = get_conversation(conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Cost is owner-private. Include last_round_cost_cents only when the caller
    # holds the owning api_key; otherwise omit so the public detail page can't
    # be used to infer per-user spend. Same key the runner billed against.
    caller_key = request.headers.get("X-API-Key", "").strip()
    is_owner = bool(caller_key) and caller_key == row["api_key"]

    body = {
        "conversation_id": row["conversation_id"],
        "topic": row["topic"],
        "status": row["status"],
        "agent_count": row["agent_count"],
        "round_count": row["round_count"],
        "persona_mix": row["persona_mix"],
        "model_id": row["model_id"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "comment_count": row["comment_count"],
        "sim_upvotes": row["sim_upvotes"],
        "progress": get_progress(conversation_id, after=after),
    }
    if is_owner:
        body["last_round_cost_cents"] = row["last_round_cost_cents"]
        body["total_cost_cents"] = row["total_cost_cents"]
    return body


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
    """Run the next round. Conversation must be paused.

    Gates on balance (estimate); the actual cost is reconciled by the runner
    after the round completes, from real per-call usage.cost.
    """
    api_key = require_api_key(request)
    if runner.is_busy(conversation_id):
        raise HTTPException(status_code=409, detail="Round already in progress")
    row = get_conversation(conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if row["round_count"] >= runner.MAX_ROUNDS:
        raise HTTPException(status_code=409, detail=f"Max {runner.MAX_ROUNDS} rounds reached")
    # Gate using the tier this sim was created on — sonnet rounds need a
    # bigger balance than flash. The tier is locked per sim (row["model_id"]).
    tier = get_tier(row["model_id"])
    if METERED and not has_balance(api_key, tier.estimate_cents):
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient balance. {tier.name} round needs ~{tier.estimate_cents}¢; "
                   f"you have {get_balance(api_key)}¢.",
        )
    if not runner.next_round(conversation_id, api_key):
        raise HTTPException(status_code=409, detail="Conversation not paused")
    return {
        "conversation_id": conversation_id,
        "status": "running",
        "balance_cents": get_balance(api_key),
    }


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
        # No refund: cost is reconciled per-round from real usage, never charged
        # upfront. A cancelled in-flight round bills its partial spend in the runner.
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
