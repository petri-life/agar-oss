# Agar

*A social simulation layer for product research.*

Agar simulates how a population of behaviorally grounded users reacts to a product
and its changes — not as independent opinions, but as a social network where
influence propagates, concerns cascade, and consensus (or backlash) emerges.

It runs a fixed population of persona-grounded agents on a simulated Hacker
News–style platform: they post, reply, upvote, and react to a product brief across
multiple rounds. The output is a threaded discussion, a population verdict, and an
HTML report.

Built on [CAMEL](https://github.com/camel-ai/camel) / OASIS as the multi-agent
simulation engine.

---

## How it works

```
product brief + persona bundles
        │
        ▼
  population assembly  ──►  agents grounded from app reviews (behavior) + forum voice (tone)
        │
        ▼
  round-by-round sim   ──►  agents post / reply / upvote on a simulated HN platform
        │
        ▼
  threaded discussion + population verdict + HTML report
```

Each agent is grounded in two things: **app-review style data** (what they care
about, how they rate things) and **forum-comment style voice** (how they actually
talk). Personas live in JSONL files — see [Personas](#personas) for the schema.

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

## License

[Apache-2.0](LICENSE). Copyright 2026 Petri.
