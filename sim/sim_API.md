# sim — API Summary

## sampler.py

- `load_profiles(path) -> list[dict]` — load JSONL (bundle or legacy format), normalize to {uid, dominant, scores, reviews, voice}
- `sample_population(profiles, n, seed?, min_per_tag?) -> list[dict]` — stratified sample by dominant friction tag
- `profile_quality(profile) -> float` — score by review count + entropy + spread

## petri_agent.py

- `PetriAgent(SocialAgent)` — SocialAgent subclass with HN env, structured per-round context
  - `__init__(..., hn_mode=True)` — swaps to HNEnvironment + HNAction after OASIS init, replaces _internal_tools
  - `rebuild_from_db(conn, current_round) -> None` — reconstruct _action_history from agent_rounds table [side-effects]
  - `perform_action_by_llm() -> response` — clears memory each round, forum-framed prompt [side-effects]

## agent_factory.py

- `build_agent_graph(profiles, brief?, model?) -> (AgentGraph, list[PetriAgent])` — build agents with review + HN voice persona [side-effects]
- `_build_persona(profile, brief?) -> str` — reviews (behavior) + voice (attitude) + brief → system prompt
- `_format_reviews(reviews, max=10) -> str` — app reviews as grounding
- `_format_voice(comments, max=10) -> str` — HN comments as forum voice

## hn_platform.py

- `HNPlatform(Platform)` — OASIS Platform subclass with nested comments
  - `running()` — overrides dispatch to handle custom actions not in ActionType enum
  - `reply_to_comment(agent_id, message) -> dict` — nested reply (parent_comment_id) [side-effects]
  - `refresh(agent_id) -> dict` — threaded comment tree observation [side-effects]
  - `_build_comment_tree(post_id) -> list[dict]` — nested comment structure from DB
- `HNAction(SocialAction)` — adds reply_to_comment tool, HN-only tool list
- `HNEnvironment(SocialEnvironment)` — threaded forum observation prompt
- `HN_ACTIONS: list[str]` — [create_post, create_comment, reply_to_comment, like_post, like_comment, do_nothing]

## state.py

- `sim_dir(base, sim_id) -> Path` — get/create simulation directory
- `db_path(base, sim_id) -> Path` — live DB path
- `tag(base, sim_id, tag_name) -> Path` — snapshot live DB to named tag
- `revert(base, sim_id, tag_name) -> Path` — restore live DB from tag
- `list_tags(base, sim_id) -> list[str]` — list available tags

## runner.py

- `SimConfig` — sim_base, rounds, convergence_rounds, model
- `SimState` — sim_id, config, tagger, env, agents, product_agent, current_round, status
- `create_simulation(sim_id, brief, profiles, config) -> SimState` — init HNPlatform + agents [side-effects]
- `resume_simulation(sim_id, brief, profiles, config, current_round) -> SimState` — rebuild from existing DB [side-effects]
- `inject_post(state, content) -> None` — post from product_team agent [side-effects]
- `advance(state, rounds?, until_converge?) -> (int, bool)` — run rounds, return (executed, converged) [side-effects]
- `tag_state(state, tag_name) -> str` — tag current state [side-effects]
- `revert_state(state, tag_name) -> str` — revert to tagged state [side-effects]
- `close(state) -> None` — shut down env [side-effects]

## tagger.py

- `Tagger` — lazy-loading friction tagger using sentence embeddings
  - `tag_new_comments(db, batch_size?) -> int` — embed untagged comments, populate comment_tags [side-effects]
- `dominant_tag(db, comment_id) -> str | None` — highest-scoring tag for a comment

## analyzer.py

- `classify_round(db, round_num, total_agents) -> dict` — classify agents, populate agent_rounds + round_signals [side-effects]
- `format_highlight(summary) -> str` — one-line terminal highlight

## synthesizer.py

- `synthesize(db, agent_profiles, total_rounds, model?, llm_timeout?) -> PopulationVerdict` — per-agent collapse + aggregation + LLM narrative [side-effects: 1 LLM call]
- `save_synthesis(verdict, dest) -> None` — write synthesis.json [side-effects]
- `AgentVerdict` — user_id, dominant_profile, trajectory, stance, dominant_concern, score, rounds
- `PopulationVerdict` — adoption_score, top_concerns, segments, trajectory_summary, narrative, agent_verdicts

## render.py

- `render_report(verdict, db) -> str` — PopulationVerdict + DB → markdown report (pure)

## html_report.py

- `generate_html_report(verdict, db, dest) -> Path` — standalone HTML with HN thread view + sparkline + verdict [side-effects]

## session.py

- `Session` — simulation lifecycle and persistence
  - `Session.create(data_path, brief, population?, seed?, model?, timeout?) -> Session` [side-effects]
  - `Session.load(session_id) -> Session`
  - `Session.list_sessions() -> list[dict]`
  - `Session.fork(source_session_id, tag, name?) -> Session` — copy-on-fork branching [side-effects]
  - `run(rounds?, until_converge?) -> int` [side-effects]
  - `inject(content) -> None` [side-effects]
  - `tag(name) -> None` [side-effects]
  - `revert(tag_name) -> None` [side-effects]
  - `report(tag_name?) -> Path` — generates HTML + markdown + synthesis.json [side-effects]
  - `status() -> dict`
  - `close() -> None` [side-effects]

## cli.py (root)

- Subcommands: `new`, `run`, `tag`, `inject`, `fork`, `revert`, `report`, `status`, `ls`
- Entry point: `uv run agar <command>`
