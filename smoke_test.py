"""OASIS framework smoke test.

Verifies: import, agent creation, env init, reset, manual step, DB state.
No LLM calls — uses ManualAction only.
"""

import asyncio
import os
import tempfile

import oasis
from camel.models import ModelFactory
from camel.types import ModelPlatformType
from oasis import (
    ActionType,
    AgentGraph,
    DefaultPlatformType,
    ManualAction,
    SocialAgent,
    UserInfo,
)


async def run():
    db_dir = tempfile.mkdtemp(prefix="agar_smoke_")
    db_path = os.path.join(db_dir, "smoke.db")

    stub_model = ModelFactory.create(ModelPlatformType.STUB, "stub")

    # Build a minimal agent graph (3 agents, stub model — no LLM calls)
    graph = AgentGraph()
    agents = []
    for i in range(3):
        info = UserInfo(
            name=f"agent_{i}",
            description=f"Test agent {i}",
            profile={
                "nodes": [],
                "edges": [],
                "other_info": {
                    "user_profile": f"I am test agent {i}.",
                    "mbti": "INTJ",
                    "gender": "unknown",
                    "age": 25,
                    "country": "US",
                },
            },
            recsys_type="reddit",
        )
        agent = SocialAgent(
            agent_id=i,
            user_info=info,
            agent_graph=graph,
            model=stub_model,
        )
        graph.add_agent(agent)
        agents.append(agent)

    # Create environment with Reddit platform
    env = oasis.make(
        agent_graph=graph,
        platform=DefaultPlatformType.REDDIT,
        database_path=db_path,
    )

    print(f"[1/4] env created, db_path={db_path}")

    # Reset (initializes platform DB)
    await env.reset()
    assert os.path.exists(db_path), "DB file not created after reset"
    print("[2/4] env.reset() OK, DB exists")

    # Manual step: agent_0 creates a post
    await env.step({
        agents[0]: ManualAction(
            action_type=ActionType.CREATE_POST,
            action_args={"content": "Hello from smoke test"},
        )
    })
    print("[3/4] manual CREATE_POST step OK")

    # Manual step: agent_1 creates a comment (need post_id=1)
    await env.step({
        agents[1]: ManualAction(
            action_type=ActionType.CREATE_COMMENT,
            action_args={"post_id": 1, "content": "Replying to smoke test"},
        )
    })
    print("[4/4] manual CREATE_COMMENT step OK")

    await env.close()

    # Verify DB has data
    import sqlite3
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"\nDB tables: {tables}")

    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
        if count > 0:
            print(f"  {table}: {count} rows")

    conn.close()
    print("\n--- SMOKE TEST PASSED ---")


if __name__ == "__main__":
    asyncio.run(run())
