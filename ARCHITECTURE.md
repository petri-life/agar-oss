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
