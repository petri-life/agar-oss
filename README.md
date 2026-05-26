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

Endpoints (all under `X-API-Key`):

```
POST   /tokens                          mint an anonymous token
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
| `AGAR_MODEL` | `haiku` | Model backend tier |
| `OPENROUTER_API_KEY` | — | Enables the OpenRouter backend when set |
| `AGAR_OPENROUTER_MODEL` | `google/gemini-2.5-flash` | OpenRouter model |
| `AGAR_DEFAULT_CREDITS` | `20` | Credits granted to a new token |
| `AGAR_MAX_CONCURRENT` | `1` | Max simultaneous simulations |
| `AGAR_ROUNDS` / `AGAR_MAX_ROUNDS` | `5` / `10` | Default / max rounds |
| `AGAR_CORS_ORIGINS` | `*` | Comma-separated allowed origins (lock down in production) |
| `AGAR_HTTP_REFERER` / `AGAR_APP_TITLE` | — / `Agar` | OpenRouter attribution headers |
| `AGAR_SARC_PATH` etc. | `personas/personas_*.jsonl` | Override persona file locations |

---

## Deploy

Example deployment configs live in [`deploy/examples/`](deploy/examples/):

- `fly.toml` — [Fly.io](https://fly.io)
- `docker-compose.yml` — Docker

A `Dockerfile` is included at the repo root.

---

## License

[Apache-2.0](LICENSE). Copyright 2026 Petri.
