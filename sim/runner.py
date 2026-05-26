"""Simulation orchestrator.

Single orchestration point: init → advance → tag → inject → report.
Binds sampler, agent_factory, and state modules.
"""

import asyncio
import logging
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import oasis
from oasis import ActionType, LLMAction, ManualAction
from oasis.social_agent.agents_generator import connect_platform_channel
from oasis.social_platform.channel import Channel

from sim.agent_factory import build_agent_graph
from sim.hn_platform import HNPlatform
from sim.state import db_path, list_tags, revert, sim_dir, tag

log = logging.getLogger("agar.runner")


@dataclass
class SimConfig:
    """Configuration for a simulation run."""

    sim_base: str = "data/simulations"
    rounds: int = 5
    convergence_rounds: int = 2
    activation: float = 1.0  # ratio of agents active per round (0.0-1.0)
    model: object = None  # camel BaseModelBackend
    analyze: bool = True  # run tagger + classifier after each round


@dataclass
class SimState:
    """Runtime state of a running simulation."""

    sim_id: str
    config: SimConfig
    tagger: object = None  # Tagger instance or None when analyze=False
    env: object = None
    agents: list = field(default_factory=list)
    product_agent: object = None
    current_round: int = 0
    status: str = "init"


async def create_simulation(
    sim_id: str,
    brief: str,
    profiles: list[dict],
    config: SimConfig,
) -> SimState:
    """Initialize a simulation from pre-sampled profiles.

    Returns a SimState ready for advance().
    """
    log.info("Creating simulation %s (%d agents, %d rounds)", sim_id, len(profiles), config.rounds)

    graph, agents = build_agent_graph(profiles, brief=brief, model=config.model)

    # Product team agent — used for injecting briefs and changes
    product_info = oasis.UserInfo(
        name="product_team",
        description="Official product team account",
        profile={
            "nodes": [],
            "edges": [],
            "other_info": {
                "user_profile": "Official product team account.",
                "mbti": "ENTJ",
                "gender": "unknown",
                "age": 30,
                "country": "US",
            },
        },
        recsys_type="reddit",
    )
    product_agent = oasis.SocialAgent(
        agent_id=len(agents),
        user_info=product_info,
        agent_graph=graph,
        model=config.model,
    )
    graph.add_agent(product_agent)

    live_db = db_path(config.sim_base, sim_id)
    channel = Channel()
    platform = HNPlatform(db_path=str(live_db), channel=channel)
    env = oasis.make(
        agent_graph=graph,
        platform=platform,
    )
    await env.reset()

    # Mark product_team for OP labeling — agents share one HNEnvironment
    for agent in agents:
        if hasattr(agent.env, 'product_user_id'):
            agent.env.product_user_id = product_agent.agent_id
            break

    tagger = None
    if config.analyze:
        from sim.tagger import Tagger
        tagger = Tagger()

    state = SimState(
        sim_id=sim_id,
        config=config,
        tagger=tagger,
        env=env,
        agents=agents,
        product_agent=product_agent,
    )
    state.status = "ready"
    log.info("Simulation %s ready (%d agents, analyze=%s)", sim_id, len(agents), config.analyze)
    return state


async def resume_simulation(
    sim_id: str,
    brief: str,
    profiles: list[dict],
    config: SimConfig,
    current_round: int = 0,
) -> SimState:
    """Resume a simulation from an existing DB.

    Rebuilds agent graph and in-memory state without re-signing up
    agents (they already exist in the DB).

    Args:
        current_round: The session's authoritative round counter.
            Agent state is rebuilt from agent_rounds table; this value
            sets the global round counter for future advance() calls.
    """
    live_db = db_path(config.sim_base, sim_id)
    if not live_db.exists():
        raise FileNotFoundError(f"No DB at {live_db} — cannot resume")

    graph, agents = build_agent_graph(profiles, brief=brief, model=config.model)

    product_info = oasis.UserInfo(
        name="product_team",
        description="Official product team account",
        profile={
            "nodes": [],
            "edges": [],
            "other_info": {
                "user_profile": "Official product team account.",
                "mbti": "ENTJ",
                "gender": "unknown",
                "age": 30,
                "country": "US",
            },
        },
        recsys_type="reddit",
    )
    product_agent = oasis.SocialAgent(
        agent_id=len(agents),
        user_info=product_info,
        agent_graph=graph,
        model=config.model,
    )
    graph.add_agent(product_agent)

    channel = Channel()
    platform = HNPlatform(db_path=str(live_db), channel=channel)
    env = oasis.make(
        agent_graph=graph,
        platform=platform,
    )

    # Start platform without re-signing up agents
    env.platform_task = asyncio.create_task(env.platform.running())
    connect_platform_channel(channel=env.channel, agent_graph=graph)

    # Mark product_team for OP labeling
    for agent in agents:
        if hasattr(agent.env, 'product_user_id'):
            agent.env.product_user_id = product_agent.agent_id
            break

    # Rebuild each PetriAgent's in-memory state from DB
    conn = sqlite3.connect(str(live_db))
    conn.row_factory = sqlite3.Row
    for agent in agents:
        agent.rebuild_from_db(conn, current_round)
    conn.close()

    tagger = None
    if config.analyze:
        from sim.tagger import Tagger
        tagger = Tagger()

    state = SimState(
        sim_id=sim_id,
        config=config,
        tagger=tagger,
        env=env,
        agents=agents,
        product_agent=product_agent,
        current_round=current_round,
    )
    state.status = "ready"
    log.info("Resumed simulation %s at round %d (%d agents)", sim_id, current_round, len(agents))
    return state


async def inject_post(state: SimState, content: str) -> None:
    """Inject a post from the product team agent."""
    log.info("Injecting post into sim %s", state.sim_id)
    await state.env.step({
        state.product_agent: ManualAction(
            action_type=ActionType.CREATE_POST,
            action_args={"content": content},
        )
    })


async def advance(
    state: SimState,
    rounds: int | None = None,
    until_converge: bool = False,
    on_step: callable = None,
    on_round: callable = None,
) -> tuple[int, bool]:
    """Run advance rounds where all agents act via LLM.

    Args:
        state: Active simulation state.
        rounds: Max rounds to run. Uses config.rounds if None.
        until_converge: If True, run up to `rounds` but stop early on convergence.

    Returns:
        (rounds_executed, converged)
    """
    rounds = rounds or state.config.rounds
    state.status = "running"
    inactive_streak = 0
    converged = False

    activation = state.config.activation
    rng = random.Random()
    sequential = activation == 0

    for i in range(rounds):
        state.current_round += 1

        if sequential:
            # Sequential mode: one agent at a time, each sees prior results
            order = list(state.agents)
            rng.shuffle(order)
            log.info("Sim %s round %d/%d (sequential, %d agents)",
                     state.sim_id, state.current_round, state.current_round - 1 + rounds,
                     len(order))
            for agent in order:
                await state.env.step({agent: LLMAction()})
                if on_step:
                    on_step(state)
        else:
            # Batch mode: select subset of agents
            if activation >= 1.0:
                active = state.agents
            else:
                k = max(1, int(len(state.agents) * activation))
                active = rng.sample(state.agents, k)

            log.info("Sim %s round %d/%d (%d/%d active)",
                     state.sim_id, state.current_round, state.current_round - 1 + rounds,
                     len(active), len(state.agents))

            actions = {agent: LLMAction() for agent in active}
            await state.env.step(actions)

        # Tag comments and classify agents (optional — skipped for lightweight API runs)
        summary = None
        highlight = "done"
        if state.config.analyze and state.tagger:
            from sim.analyzer import classify_round, format_highlight
            live_db = str(db_path(state.config.sim_base, state.sim_id))
            tagged = state.tagger.tag_new_comments(live_db)
            summary = classify_round(live_db, state.current_round, len(state.agents))
            highlight = format_highlight(summary)
            log.info("Sim %s round %d complete — %s (tagged %d comments)",
                     state.sim_id, state.current_round, highlight, tagged)
        else:
            log.info("Sim %s round %d complete", state.sim_id, state.current_round)

        if on_round:
            on_round(state.current_round, highlight)

        # Convergence detection (requires analysis)
        if until_converge and summary:
            counts = summary.get("counts", {})
            n = summary.get("total_agents", 1)
            engaged = counts.get("engaged", 0) + counts.get("repeated", 0)
            engagement_rate = engaged / n if n else 0
            if engagement_rate < 0.2:
                inactive_streak += 1
            else:
                inactive_streak = 0
            if inactive_streak >= state.config.convergence_rounds:
                converged = True
                log.info("Sim %s converged after %d rounds (engagement < 20%% for %d rounds)",
                         state.sim_id, i + 1, state.config.convergence_rounds)
                break

    state.status = "converged" if converged else "ready"
    return i + 1, converged


async def tag_state(state: SimState, tag_name: str) -> str:
    """Tag the current simulation state."""
    tag(state.config.sim_base, state.sim_id, tag_name)
    log.info("Tagged sim %s as '%s'", state.sim_id, tag_name)
    return tag_name


async def revert_state(state: SimState, tag_name: str) -> str:
    """Revert simulation to a tagged state.

    Note: after revert, the env must be re-initialized from the restored DB.
    This is a limitation — OASIS doesn't support hot-reloading state.
    """
    revert(state.config.sim_base, state.sim_id, tag_name)
    log.info("Reverted sim %s to '%s'", state.sim_id, tag_name)
    return tag_name


async def close(state: SimState) -> None:
    """Shut down the simulation environment."""
    if state.env:
        await state.env.close()
        state.env = None
    state.status = "closed"


async def run_baseline(
    sim_id: str,
    brief: str,
    profiles: list[dict],
    config: SimConfig,
) -> SimState:
    """Full baseline run: create → inject brief → advance → tag."""
    state = await create_simulation(sim_id, brief, profiles, config)
    await inject_post(state, brief)
    await advance(state)
    await tag_state(state, "baseline")
    return state


async def run_injection(
    state: SimState,
    change: str,
    tag_name: str,
    rounds: int | None = None,
) -> str:
    """Inject a change and advance: inject → advance → tag."""
    await inject_post(state, change)
    await advance(state, rounds=rounds)
    await tag_state(state, tag_name)
    return tag_name
