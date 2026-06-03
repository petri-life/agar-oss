## [v0.3.0] — 2026-05-27

### Added
- Per-round cost metering: the OpenRouter backend accumulates real `usage.cost`
  per LLM call (thread-safe across concurrent agents); the runner reconciles it
  after each round and bills the actual spend, rounded up to whole cents
- Credit top-up endpoint `POST /credits/add` (cents), gated by `X-Mint-Secret`
  for the hosted payment webhook to call after a successful charge
- `X-Mint-Secret` gate on `POST /tokens` and `POST /credits/add` via
  `AGAR_MINT_SECRET`; unset (the OSS default) leaves both open for free local use
- `balance_cents` exposed on token/conversation-start/next-round responses and
  `last_round_cost_cents` on conversation status, for live balance display
- Configurable CORS via `AGAR_CORS_ORIGINS` (defaults to `*` for local/OSS)
- OpenRouter attribution headers via `AGAR_HTTP_REFERER` / `AGAR_APP_TITLE`

### Changed
- Balances are now **cents**, not round counts. `AGAR_DEFAULT_CREDIT_CENTS`
  (default `0`) replaces the old per-round credit grant; legacy round-count
  balances migrate to cents at `AGAR_LEGACY_ROUND_CENTS` (default `10`) per round
- Estimate-then-reconcile billing: a round start gates on
  `AGAR_ROUND_ESTIMATE_CENTS` (default `10`, worst-case); the real cost is charged
  after. A started round always settles — never charged upfront, so no refunds
- A failed or cancelled round still bills the partial spend it incurred
- The Claude-CLI backend stays unmetered (it returns no cost data) — the free
  local tier. Metering activates only when `OPENROUTER_API_KEY` is set

## [v0.1.2] — 2026-04-08

### Fixed
- Persona files missing on Fly — volume mount shadowed data/, moved to personas/
- Health check timeout during sim init — simulation now runs in separate thread
- No app-level logs visible — configured Python logging to stdout

### Changed
- GitHub Actions deploy triggers on version tags only, not every push
- Sampler uses single personas/ directory, no fallback logic

## [v0.1.1] — 2026-04-08

### Fixed
- Bump VM to 1GB — OASIS+torch baseline exceeds 512MB, caused OOM on conversation start

## [v0.1.0] — 2026-04-08

### Added
- FastAPI server with conversation lifecycle (create, poll, thread, upvote, kill)
- Anonymous token auth with readable labels (coral-fox-42) and credit limits
- Population assembly: 18 spicy (always) + 18 slider-controlled (creative/adversarial)
- OpenRouter LLM backend (Gemini 2.5 Flash) as drop-in for Claude CLI
- Per-round progress polling with cursor-based pagination
- Human upvotes on comments (1 per token per comment, idempotent)
- Persona debug mode (AGAR_SHOW_PERSONAS) exposing real slugs and prompts
- Comment count and sim upvotes snapshotted to API DB on completion
- Max concurrent simulation limit with kill endpoint
- Fly.io deployment with 512MB VM, persistent volume
- Admin CLI (agar-keys) for key management by label
- Release skill for manual deploy pipeline
- Health endpoint with version and active sim count

### Changed
- Agent prompts now encourage like_comment over redundant replies
- Tagger/classifier skipped for API runs (analyze=False), saves ~300MB RAM
- Orphaned conversations auto-cleaned on server startup
