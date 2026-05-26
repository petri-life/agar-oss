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
