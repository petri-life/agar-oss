# Agar — Architecture

## What it does

Agar simulates how a population of behaviorally grounded users react to a
product brief — not as independent opinions, but as a social network where
influence propagates, concerns cascade, and silence emerges organically.

**Input:** product brief + user persona bundles (app reviews + forum voice)
**Output:** HN-style threaded discussion + population verdict + HTML report

## Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INITIALIZATION                              │
│                                                                     │
│  export_bundles.jsonl ──► sampler ──► agent_factory ──► OASIS env   │
│  (reviews + HN voice)     (stratified   (review+voice    (HN       │
│                             top-k)        persona)        platform) │
│                                                                     │
│  brief.md ──► inject_post() ──────────────────────────► round 0     │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     SIMULATION LOOP (per round)                     │
│                                                                     │
│  ┌──────────────────────────────────────────────┐                   │
│  │  PetriAgent.perform_action_by_llm()          │                   │
│  │                                              │                   │
│  │  system = persona (reviews+voice) + history  │                   │
│  │  user   = threaded forum view (HN-style)     │                   │
│  │              │                               │                   │
│  │              ▼                               │                   │
│  │  ┌────────────────────────┐                  │                   │
│  │  │  claude -p (haiku)     │ ◄── LLM call     │                   │
│  │  │  ClaudeCliModel        │     per agent     │                   │
│  │  │  tool call → action    │     per round     │                   │
│  │  └────────────────────────┘                  │                   │
│  │              │                               │                   │
│  │              ▼                               │                   │
│  │  action recorded to OASIS SQLite (trace,     │                   │
│  │  post, comment, like tables)                 │                   │
│  └──────────────────────────────────────────────┘                   │
│                     │                                               │
│                     ▼ (one step behind)                             │
│  ┌──────────────────────────────────────────────┐                   │
│  │  OBSERVABILITY                               │                   │
│  │                                              │                   │
│  │  tagger.py ─────► comment_tags table         │                   │
│  │  (all-MiniLM-L6-v2, cosine sim vs 10 tags)  │ ◄── embedding     │
│  │                                              │     (local, fast) │
│  │  analyzer.py ───► agent_rounds table         │                   │
│  │  (engaged/repeated/passive/silent per agent) │ ◄── pure Python   │
│  │                 ► round_signals table         │                   │
│  │  (population aggregate per tag per round)    │                   │
│  │                                              │                   │
│  │  stdout: "Round 3: 40% engaged, 50% silent   │                   │
│  │           | trust-failure×3"                  │                   │
│  └──────────────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         SYNTHESIS (end of run)                      │
│                                                                     │
│  ┌──────────────────────────────────────────────┐                   │
│  │  Per-agent trajectory collapse               │ ◄── pure Python   │
│  │  agent_rounds → AgentVerdict                 │     (no LLM)      │
│  │  (trajectory, stance, score, concern)        │                   │
│  └──────────────┬───────────────────────────────┘                   │
│                 │                                                    │
│                 ▼                                                    │
│  ┌──────────────────────────────────────────────┐                   │
│  │  Population aggregation                      │ ◄── pure Python   │
│  │  adoption_score, top_concerns, segments      │     (no LLM)      │
│  └──────────────┬───────────────────────────────┘                   │
│                 │                                                    │
│                 ▼                                                    │
│  ┌──────────────────────────────────────────────┐                   │
│  │  Narrative generation                        │                   │
│  │  ┌────────────────────────┐                  │                   │
│  │  │  claude -p (haiku)     │ ◄── 1 LLM call   │                   │
│  │  │  structured prompt     │     at end        │                   │
│  │  │  → 2-3 sentence verdict│                  │                   │
│  │  └────────────────────────┘                  │                   │
│  └──────────────────────────────────────────────┘                   │
│                                                                     │
│  Output: synthesis.json + report.md + report.html                   │
└─────────────────────────────────────────────────────────────────────┘
```

## Modules

```
sim/
├── sampler.py          pure    bundle/legacy JSONL → stratified population sample
├── agent_factory.py    pure    profiles (reviews+voice) → PetriAgents with persona
├── petri_agent.py      OASIS   SocialAgent subclass, HN env, per-round context + resume
├── hn_platform.py      OASIS   HNPlatform (nested comments), HNAction, HNEnvironment
├── claude_model.py     I/O     CAMEL model backend → claude CLI subprocess
├── runner.py           orch    create/resume → inject → advance → tag (HNPlatform)
├── state.py            I/O     tag/revert via SQLite file copy
├── tagger.py           embed   Tagger class, comment → friction tag scores
├── analyzer.py         pure    per-round agent classification + population aggregation
├── synthesizer.py      LLM+    per-agent collapse + population verdict + narrative
├── render.py           pure    PopulationVerdict + DB → markdown report
├── html_report.py      pure    PopulationVerdict + DB → standalone HTML (HN thread view)
└── session.py          orch    session lifecycle + fork (tree branching)

cli.py                  CLI     new, run, tag, inject, fork, revert, report, status, ls
```

## Data flow

```
Input                       Module              Output
─────                       ──────              ──────
export_bundles.jsonl    →   sampler         →   list[profile] (reviews + voice)
list[profile] + brief   →   agent_factory   →   AgentGraph + PetriAgent[]
AgentGraph              →   runner.create   →   HNPlatform env + SQLite DB
(existing DB)           →   runner.resume   →   rebuilt SimState (no re-signup)
brief / change          →   runner.inject   →   post in DB
LLMAction per agent     →   runner.advance  →   trace/post/comment (nested) in DB
                                                 (convergence detection: auto-stop)
new comments            →   tagger          →   comment_tags table
trace + comment_tags    →   analyzer        →   agent_rounds + round_signals tables
agent_rounds            →   synthesizer     →   AgentVerdict[] → PopulationVerdict
PopulationVerdict       →   save_synthesis  →   synthesis.json
PopulationVerdict + DB  →   render          →   report.md
PopulationVerdict + DB  →   html_report     →   report.html (HN thread view)
```

## Where models are used

| Call site | Model | Type | When | Count |
|-----------|-------|------|------|-------|
| PetriAgent.perform_action_by_llm | claude haiku (CLI) | LLM | every agent, every round | agents × rounds |
| Tagger.tag_new_comments | all-MiniLM-L6-v2 | embedding | after each round | 1 batch per round |
| synthesizer._call_llm | claude haiku (CLI) | LLM | end of run | 1 |

At 10 agents × 5 rounds: ~50 LLM calls + 5 embedding batches + 1 narrative call.
At 10K agents × 15 rounds: ~150K LLM calls + 15 embedding batches + 1 narrative call.

## SQLite schema (custom tables added to OASIS DB)

```sql
-- tagger.py: friction tag attribution per comment
comment_tags (
    comment_id  INTEGER NOT NULL,
    tag         TEXT NOT NULL,       -- one of 10 FRICTION_TAGS
    score       REAL NOT NULL,       -- cosine similarity 0-1
    PRIMARY KEY (comment_id, tag)
)

-- analyzer.py: per-agent per-round classification
agent_rounds (
    user_id         INTEGER NOT NULL,
    round           INTEGER NOT NULL,
    action_class    TEXT NOT NULL,    -- engaged | repeated | passive | silent
    dominant_tag    TEXT,             -- highest-scoring tag this round (nullable)
    PRIMARY KEY (user_id, round)
)

-- analyzer.py: population aggregate per round
round_signals (
    round               INTEGER NOT NULL,
    tag                 TEXT NOT NULL,
    comment_count       INTEGER NOT NULL DEFAULT 0,
    agent_count         INTEGER NOT NULL DEFAULT 0,
    engagement_rate     REAL NOT NULL DEFAULT 0,
    do_nothing_rate     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (round, tag)
)
```

## Session directory

All written to `data/sessions/{session_id}/`:

| File | Purpose |
|------|---------|
| session.json | Session config + state (round, status, tags) |
| brief.txt | Product brief (input) |
| profiles.json | Sampled agent profiles (compact) |
| profiles_full.jsonl | Full profiles for exact reproduction |
| prompts.log | Full LLM prompts per agent per round (debug) |
| sim/{session_id}/state.db | Live OASIS SQLite DB |
| sim/{session_id}/tags/*.db | Tagged snapshots |
| reports/{tag}/synthesis.json | Structured verdict |
| reports/{tag}/report.md | Markdown report |
| reports/{tag}/report.html | Standalone HTML (HN thread view + sparkline) |

## CLI

```
uv run agar new    --data bundles.jsonl --brief partiful.md --population 50
uv run agar run    sim_xxx --rounds 5
uv run agar run    sim_xxx --until-converge
uv run agar tag    sim_xxx baseline
uv run agar fork   sim_xxx baseline --name change-a
uv run agar inject sim_xxx_change-a --file cut-social-features.md
uv run agar run    sim_xxx_change-a --rounds 5
uv run agar report sim_xxx_change-a
uv run agar revert sim_xxx baseline
uv run agar status sim_xxx
uv run agar ls
```

## API layer

The CLI drives the sim directly; the API (`agar-api`) wraps it as a service that
runs **one round per request** — no long-lived background loop. `POST
/conversations` creates the session and runs round 1 in a daemon thread that
exits when the round pauses; each `POST /conversations/{id}/next` runs one more.

```
api/
├── main.py      FastAPI app, endpoints, auth gates, request validation
├── runner.py    orch    per-round thread: create/load session → run 1 round → reconcile cost → pause
├── sampler.py   pure    persona_mix → fixed 36-agent population (18 base + 18 flavor)
├── db.py        I/O     SQLite: api_keys (balances), conversations, progress, upvotes
└── thread.py    pure    OASIS DB → API thread JSON; inject human comments
```

**Two auth boundaries.** Conversation endpoints require `X-API-Key` (an
auto-provisioned anonymous token). The privileged `POST /tokens` and `POST
/credits/add` are gated by `require_mint_secret`: when `AGAR_MINT_SECRET` is set
they need a constant-time-matched `X-Mint-Secret` header; unset (OSS default)
leaves them open for free local use.

**Model backend swap.** The CLI path uses `claude_model.py` (Claude CLI). When
`OPENROUTER_API_KEY` is present the runner swaps in `sim/openrouter_model.py`,
which is the only backend that reports per-call cost.

### Cost metering (estimate-then-reconcile)

Billing exists only on the OpenRouter path; the Claude-CLI backend reports no
cost and runs unmetered (the free local tier, `METERED = False`).

```
round start ──► gate: balance ≥ AGAR_ROUND_ESTIMATE_CENTS ?   (worst-case, default 10¢)
                  │ no → 402 Insufficient balance
                  ▼ yes
              backend.reset_cost()            ← zero the accumulator
              session.run(rounds=1)           ← each LLM call adds real usage.cost (thread-safe)
              spent = backend.reset_cost()    ← read accumulated USD
              cents = ceil(spent × 100)        ← round UP, never under-bill
              decrement_cents(api_key, cents) ← settle; clamp at 0
              store last_round_cost_cents
```

A round that *starts* always settles its bill — cost is reconciled after, never
charged upfront, so there are no refunds. A failed or cancelled in-flight round
still bills its partial spend (the calls already cost money).

Balances live in `api_keys.credits_remaining` as **cents** (`credits_unit =
'cents'`). `init_db` migrates any legacy round-count balance by multiplying it by
`AGAR_LEGACY_ROUND_CENTS`. `POST /credits/add` tops up out of band — called by a
trusted payment webhook holding the mint secret, not the browser.

### API SQLite schema (separate DB from the per-session OASIS DBs)

```sql
api_keys (
    key              TEXT PRIMARY KEY,
    label            TEXT DEFAULT 'anon',  -- readable, e.g. coral-fox-42
    created_at       TEXT NOT NULL,
    revoked          INTEGER DEFAULT 0,
    credits_remaining INTEGER DEFAULT 3,   -- balance; unit per credits_unit
    credits_unit     TEXT DEFAULT 'rounds' -- added by migration; flipped to 'cents'
)                                          -- (× AGAR_LEGACY_ROUND_CENTS) exactly once

conversations (
    conversation_id       TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,    -- 'pending' until the sim DB is created
    api_key               TEXT NOT NULL,
    topic                 TEXT NOT NULL,
    status                TEXT DEFAULT 'queued', -- queued → running → paused → done | failed
    agent_count           INTEGER NOT NULL,
    round_count           INTEGER DEFAULT 0,
    persona_mix           REAL DEFAULT 0.5,
    created_at            TEXT NOT NULL,
    started_at            TEXT,
    finished_at           TEXT,
    error                 TEXT,
    comment_count         INTEGER DEFAULT 0, -- snapshotted from the OASIS DB per round
    sim_upvotes           INTEGER DEFAULT 0,
    last_round_cost_cents INTEGER DEFAULT 0
)                                            -- list view derives `score` = COUNT(upvotes)
```
