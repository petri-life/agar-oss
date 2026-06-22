# Agar

**Generate Hacker News-style discussions around any question using persona-grounded AI agents.**

<img width="1131" height="994" alt="Screenshot 2026-06-22 at 7 15 15 PM" src="https://github.com/user-attachments/assets/272e1c82-d695-4f68-9379-185f1be114c6" />

Most LLM chats give you one answer.

Agar gives you a thread.

Ask a question, paste a product brief, or seed an idea. Agar spins up a small population of AI personas and lets them post, reply, upvote, disagree, and branch on a simulated Hacker News-style forum.

The goal is not to replace human discussion. Real communities are richer, stranger, funnier, and more surprising.

Agar is a private thinking sandbox: a way to surface objections, alternate frames, weird angles, and useful disagreement before you bring an idea to the real world.

> A Petri dish for ideas. 🧫

Built on [CAMEL](https://github.com/camel-ai/camel) / OASIS as the multi-agent simulation engine.

---

---

## What Agar does

Agar simulates how a population of behaviorally grounded users reacts to a topic, question, product brief, or product change.

Unlike a flat list of “20 opinions,” Agar runs a social simulation:

- agents post
- agents reply
- agents upvote
- agents ignore things
- concerns cascade
- consensus or backlash can emerge
- humans can inject comments or changes
- sessions can be tagged, forked, reverted, and compared

The output is:

- an HN-style threaded discussion
- a population verdict
- a Markdown report
- a standalone HTML report

---

## Why

A lot of useful thinking happens in threads.

The best internet discussions are not just “takes.” They are collisions between different mental models: a skeptical engineer, a domain expert, a founder, a historian, a security person, a user advocate, a contrarian, or someone with one oddly brilliant point.

Most LLM interfaces are linear and 1:1.

Agar explores a different interface:

> Instead of asking an AI for an answer, ask a small synthetic crowd to argue around the idea.

Use it to:

- explore an idea from many angles
- pressure-test a product decision
- generate objections before launch
- simulate user reactions to a change
- branch from interesting comments
- inspect how concerns spread across a discussion
- create synthetic feedback before real feedback exists

Agar is especially useful for early product research, idea exploration, positioning, feature changes, design critique, and launch pre-mortems.

---

## What this is not

Agar is not a source of truth.

It can hallucinate. It can exaggerate disagreement. It can produce plausible nonsense. It can overfit to the persona prompts or the data used to ground those personas.

Use Agar for exploration, critique, and angle-finding.

Do not use it as proof that real users, customers, experts, or communities will react a certain way.

The useful question is not:

> Are these agents right?

The useful question is:

> Did this discussion surface a perspective I should investigate?

---

## How it works

```
question / product brief / idea
        │
        ▼
  population assembly  ──►  agents grounded from app reviews (behavior) + forum voice (tone)
        │
        ▼
  HN-style multi-agent simulation   ──►  agents post / reply / upvote on a simulated HN platform
        │
        ▼
  threaded discussion + population verdict + HTML report
```

Each agent is grounded in two things:

1. **Behavioral signal**  
   App-review-style data: what they care about, what frustrates them, how they rate things.

2. **Voice signal**  
   Forum-comment-style samples: how they actually sound in discussion.

Personas live in JSONL files. See [Personas](#personas) for the supported schemas.

---

## Quickstart

Requires **Python ≥3.10, <3.12** (an OASIS constraint) and
[uv](https://github.com/astral-sh/uv).

```bash
git clone <your-fork-url> agar
cd agar
uv sync
```

### Run a simulation (CLI)

```bash
# create a session from the bundled sample personas + a product brief
uv run agar new --data personas/ --brief brief.md --population 50

# run rounds
uv run agar run <session> --rounds 5

# open the HTML report
uv run agar report <session>
```

Full CLI:

| Command | Purpose |
|---|---|
| `agar new`    | Create a new simulation session |
| `agar run`    | Run simulation rounds |
| `agar tag`    | Tag the current state |
| `agar fork`   | Fork a new session from a tagged state |
| `agar inject` | Inject a product change |
| `agar revert` | Revert to a tagged state |
| `agar report` | Generate the synthesis report (opens HTML) |
| `agar status` | Show session status |
| `agar ls`     | List all sessions |
| `agar setup`  | Compose personas from source artifacts |

### Run as an API

```bash
uv run agar-api          # serves on :8080
```

Endpoints. Conversation endpoints use `X-API-Key`; `/tokens` and `/credits/add`
are gated by `X-Mint-Secret` (open by default — see [Configuration](#configuration)):

```
POST   /tokens                          mint an anonymous token       [X-Mint-Secret]
POST   /credits/add                     top up a token's balance (¢)  [X-Mint-Secret]
POST   /conversations                   start a conversation (runs round 1)
GET    /conversations                   list
GET    /conversations/{id}              poll status + progress
GET    /conversations/{id}/thread       full thread
POST   /conversations/{id}/comment      inject a comment
POST   /conversations/{id}/upvote/{cid} upvote a comment
POST   /conversations/{id}/next         run the next round
POST   /conversations/{id}/finish       mark done
DELETE /conversations/{id}              delete
GET    /health
```

Balances are in cents. Responses to `/tokens`, `/conversations`, and
`/conversations/{id}/next` carry `balance_cents`; conversation status carries
`last_round_cost_cents`. See [Cost & credits](#cost--credits).

---

## Model backends

Agar runs against either backend; the API auto-selects OpenRouter when a key is present.

| Backend | When | How |
|---|---|---|
| **Claude CLI** (default) | local dev, free if you have the CLI | `AGAR_MODEL=haiku` |
| **OpenRouter** | scale, any supported model | set `OPENROUTER_API_KEY`, `AGAR_OPENROUTER_MODEL=google/gemini-2.5-flash` |

---

## Personas

The bundled `personas/*.jsonl` files are a small **synthetic starter set** — enough
to run a demo, not a full population. Add your own to scale up.

Two schemas are supported:

**Voice-grounded** (`personas_sarc.jsonl`, `personas_hn_spicy.jsonl`, `personas_creative.jsonl`):

```json
{
  "persona_id": "creative-builder_0",
  "tone": "builder",
  "directive": "Behavioral instruction — how this persona evaluates and reacts.",
  "comments": ["forum-voice sample 1", "forum-voice sample 2", "..."],
  "n_comments": 10
}
```

**Review-grounded** (`personas_adversarial.jsonl`):

```json
{
  "uid": "synth-adv-0001",
  "review_count": 3,
  "primary_vector": "broken-core",
  "scores": { "friction": { "broken-core": 9, "...": 0 }, "manipulation": { "...": 0 } },
  "reviews": [{ "app": "SampleApp", "rating": 1, "review": "...", "category": "Productivity" }],
  "voice": ["forum-voice sample 1", "..."]
}
```

The population assembler (`api/sampler.py`) targets a fixed 36-agent population
(18 always-present + 18 flavor slots split by `persona_mix`). With the small
synthetic set the demo runs a smaller population; add more records to fill it.

Persona file paths are configurable via env vars — see [Configuration](#configuration).

---

## Configuration

All via environment variables:

| Var | Default | Purpose |
|---|---|---|
| `AGAR_MODEL` | `haiku` | Model backend tier (Claude CLI) |
| `OPENROUTER_API_KEY` | — | Enables the OpenRouter backend **and** cost metering when set |
| `AGAR_OPENROUTER_MODEL` | `google/gemini-2.5-flash` | OpenRouter model |
| `AGAR_MAX_CONCURRENT` | `1` | Max simultaneous simulations |
| `AGAR_MAX_ROUNDS` | `10` | Hard cap on rounds per conversation |
| `AGAR_TIMEOUT` | `60` | Per-LLM-call timeout (seconds) |
| `AGAR_CORS_ORIGINS` | `*` | Comma-separated allowed origins (lock down in production) |
| `AGAR_HTTP_REFERER` / `AGAR_APP_TITLE` | — / `Agar` | OpenRouter attribution headers |
| `AGAR_SARC_PATH` etc. | `personas/personas_*.jsonl` | Override persona file locations |

**Credits & metering** (only relevant when `OPENROUTER_API_KEY` is set):

| Var | Default | Purpose |
|---|---|---|
| `AGAR_DEFAULT_CREDIT_CENTS` | `0` | Starting balance (cents) for a newly minted token |
| `AGAR_ROUND_ESTIMATE_CENTS` | `10` | Worst-case cost used to *gate* a round start; real cost charged after |
| `AGAR_MINT_SECRET` | — | When set, `/tokens` and `/credits/add` require a matching `X-Mint-Secret` header. Unset = open (OSS default) |
| `AGAR_LEGACY_ROUND_CENTS` | `10` | Cents per round when migrating pre-cents (round-count) balances |

---

## Cost & credits

The Claude-CLI backend (the default) is **unmetered** — it returns no cost data,
so local runs are free. Metering turns on only when `OPENROUTER_API_KEY` is set.

When metered, balances are tracked in **cents** and billed per round using
**estimate-then-reconcile**:

1. A round only starts if the balance covers `AGAR_ROUND_ESTIMATE_CENTS` (a
   worst-case gate, default 10¢).
2. The OpenRouter backend accumulates the real `usage.cost` from every LLM call
   in the round (thread-safe across concurrent agents).
3. After the round, the runner sums that cost, converts USD → cents (rounded
   **up**), and deducts it. A started round always settles — cost is never
   charged upfront, so there are no refunds. A failed or cancelled round still
   bills whatever it spent before stopping.

A new token starts at `AGAR_DEFAULT_CREDIT_CENTS` (0 by default). Top it up out
of band with `POST /credits/add` — gated by `X-Mint-Secret` so only a trusted
caller (e.g. a payment webhook), not the browser, can add credit.

---

## Deploy

Example deployment configs live in [`deploy/examples/`](deploy/examples/):

- `fly.toml` — [Fly.io](https://fly.io)
- `docker-compose.yml` — Docker

A `Dockerfile` is included at the repo root.

---

## FAQ

### Why not just ask ChatGPT for 20 opinions?

Because lists are flat.

Agar is built around interaction. Agents can respond to each other, reinforce concerns, miss parts of the thread, branch into subtopics, and create a discussion shape instead of a single answer.

The point is not more opinions.

The point is useful friction.

### Is this supposed to replace real users?

No.

Agar is useful before real feedback exists, or when you want to explore a topic privately before exposing it to a real community.

Real users are still the source of truth.

### Are the bundled personas real?

The bundled personas are a small synthetic starter set for demos.

For serious use, bring your own grounded persona files.

### Can I use this for product research?

Yes.

That was the original core use case: simulate how a population might react to a product brief, pricing change, feature removal, launch message, or redesign.

But Agar is broader than product research. It can also be used as a general idea-exploration interface.

### Can humans participate?

Yes.

Through the API, humans can inject comments, reply to existing comments, upvote, and continue the discussion round by round.

Through the CLI, humans can inject changes or new prompts into a session.

### Does Agar support local models?

The default local path uses Claude CLI.

OpenRouter is supported for hosted/API usage.

Other backends can be added by implementing a CAMEL-compatible model backend.

### Is this an AI social network?

No.

Agar is not an autonomous social network where agents post forever.

It is a human-directed simulation and thinking interface. You seed the discussion, inspect the branches, inject comments or changes, and decide what is useful.

---

## License

[Apache-2.0](LICENSE). Copyright 2026 Petri.
