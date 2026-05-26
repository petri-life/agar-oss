"""PetriAgent — SocialAgent subclass with structured self-memory.

Replaces OASIS's flat message accumulation with a clean per-round context:

  system = persona (static) + compact action history (growing, structured)
  user   = current env snapshot only (ephemeral, replaced each round)

OASIS's ChatHistoryMemory is cleared before every LLM call so the context
never balloons with stacked env JSON dumps. The agent's own action history
is maintained as a typed list and rendered compactly into the system prompt.
"""

import sqlite3
from dataclasses import dataclass

from camel.messages import BaseMessage
from oasis import SocialAgent, AgentGraph, UserInfo
from oasis.social_agent.agent import ActionType, agent_log, ALL_SOCIAL_ACTIONS


@dataclass
class ActionRecord:
    round: int
    action: str
    content: str  # empty string for do_nothing / refresh / like


def _render_history(records: list[ActionRecord]) -> str:
    """Render action history as a compact block for the system prompt."""
    if not records:
        return "None yet."
    lines = []
    for r in records:
        if r.content:
            lines.append(f"[Round {r.round}] {r.action}: {r.content[:300]}")
        else:
            lines.append(f"[Round {r.round}] {r.action}")
    return "\n".join(lines)


HISTORY_BLOCK = """\

--- Your actions so far (do NOT repeat yourself — if you've already made a \
point, engage with something genuinely new or use do_nothing) ---
{history}
---"""



class PetriAgent(SocialAgent):
    """SocialAgent with clean per-round context and structured action history.

    Each round:
    1. Render action history into system prompt.
    2. Clear CAMEL memory (reset accumulation).
    3. Re-seed memory with updated system message.
    4. Call astep() with only the current env snapshot.
    5. Record the action taken.
    """

    def __init__(
        self,
        agent_id: int,
        user_info: UserInfo,
        base_system_content: str,
        user_info_template=None,
        channel=None,
        model=None,
        agent_graph: AgentGraph = None,
        available_actions: list[ActionType] = None,
        tools=None,
        max_iteration: int = 1,
        hn_mode: bool = True,
    ):
        super().__init__(
            agent_id=agent_id,
            user_info=user_info,
            user_info_template=user_info_template,
            channel=channel,
            model=model,
            agent_graph=agent_graph,
            available_actions=available_actions,
            tools=tools,
            max_iteration=max_iteration,
        )

        # Swap to HN environment + actions after OASIS init
        if hn_mode and self.channel:
            from sim.hn_platform import HNAction, HNEnvironment, HN_ACTIONS
            self.env = HNEnvironment(HNAction(agent_id, self.channel))
            # Replace ChatAgent's internal tools with HN-only set
            hn_tools = self.env.action.get_openai_function_list()
            self._internal_tools = {
                t.get_function_name(): t for t in hn_tools
            }

        # Static persona content, without history block
        self._base_system_content: str = base_system_content
        self._action_history: list[ActionRecord] = []
        self._current_round: int = 0

    def rebuild_from_db(self, conn: sqlite3.Connection, current_round: int) -> None:
        """Reconstruct in-memory action history from OASIS trace + comment tables.

        Reads the trace table (always present) to find what this agent did,
        then fetches comment content for comment/reply actions.
        """
        user_id = self.agent_id
        self._action_history = []

        # Get this agent's comments with content
        comments = conn.execute(
            "SELECT comment_id, content FROM comment WHERE user_id = ? ORDER BY comment_id",
            (user_id,),
        ).fetchall()

        # Get this agent's trace actions
        try:
            traces = conn.execute(
                "SELECT action, info FROM trace WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            traces = []

        comment_idx = 0
        round_num = 0
        for action, info in traces:
            if action in ("refresh", "sign_up", "update_rec"):
                # refresh marks a new round
                if action == "refresh":
                    round_num += 1
                continue

            if action in ("create_comment", "reply_to_comment"):
                content = ""
                if comment_idx < len(comments):
                    content = comments[comment_idx][1]
                    comment_idx += 1
                self._action_history.append(ActionRecord(
                    round=round_num or 1,
                    action=action,
                    content=content[:300] if content else "",
                ))
            elif action in ("like_comment", "like_post", "dislike_comment", "dislike_post"):
                self._action_history.append(ActionRecord(
                    round=round_num or 1,
                    action=action,
                    content="",
                ))
            elif action == "do_nothing":
                self._action_history.append(ActionRecord(
                    round=round_num or 1,
                    action="do_nothing",
                    content="",
                ))

        self._current_round = current_round

    def _build_system_message(self) -> BaseMessage:
        history_text = _render_history(self._action_history)
        content = self._base_system_content + HISTORY_BLOCK.format(
            history=history_text
        )
        return BaseMessage.make_assistant_message(
            role_name="system",
            content=content,
        )

    def _record_action(self, action_name: str, args: dict) -> None:
        content = args.get("content", "") or args.get("comment", "")
        self._action_history.append(ActionRecord(
            round=self._current_round,
            action=action_name,
            content=content,
        ))

    async def perform_action_by_llm(self):
        self._current_round += 1

        # Rebuild system message with latest history
        updated_system = self._build_system_message()
        self._system_message = updated_system

        # Clear accumulated CAMEL memory — re-seeds from self._system_message
        self.clear_memory()

        # Set context mode for this agent's observation
        if hasattr(self.env, 'comments_only'):
            self.env.comments_only = getattr(self, 'context_mode', 'full') == 'comments-only'

        # Current env snapshot only — no stacked prior rounds
        env_prompt = await self.env.to_text_prompt()

        user_msg = BaseMessage.make_user_message(
            role_name="User",
            content=(
                f"Here is the current forum discussion.\n\n{env_prompt}\n\n"
                f"Actions: create_comment (new top-level comment on the post), "
                f"reply_to_comment (reply to a specific comment), "
                f"like_comment (upvote a comment you agree with), "
                f"do_nothing.\n\n"
                f"If someone already made your point, like_comment instead "
                f"of writing a similar comment.\n"
                f"Prefer create_comment — share your own angle on the "
                f"original post. reply_to_comment only when you disagree "
                f"or have a concrete detail to add.\n"
                f"Before do_nothing, check if any comment deserves a "
                f"like_comment. Upvoting good comments is always useful.\n"
                f"Comments from [OP] are from the original poster — "
                f"pay attention and consider responding to them.\n\n"
                f"Avoid:\n"
                f"- Addressing the group (\"everyone's...\", \"you're all...\", \"nobody mentioned...\")\n"
                f"- Referencing other users (\"User 3 is right\", \"as someone said\")\n"
                f"- Summarizing or refereeing the discussion\n"
                f"- Repeating or rephrasing anything in your action history\n\n"
                f"If you're responding to someone, prefer reply_to_comment "
                f"over a top-level comment that references them."
            ),
        )

        try:
            agent_log.info(
                f"Agent {self.social_agent_id} observing environment "
                f"(round {self._current_round})"
            )
            response = await self.astep(user_msg)
            for tool_call in response.info["tool_calls"]:
                action_name = tool_call.tool_name
                args = tool_call.args
                self._record_action(action_name, args)
                agent_log.info(
                    f"Agent {self.social_agent_id} performed "
                    f"action: {action_name} with args: {args}"
                )
                if action_name not in ALL_SOCIAL_ACTIONS:
                    agent_log.info(
                        f"Agent {self.social_agent_id} get the result: "
                        f"{tool_call.result}"
                    )
                return response
        except Exception as e:
            agent_log.error(f"Agent {self.social_agent_id} error: {e}")
            return e
