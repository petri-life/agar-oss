"""HackerNews-style platform for OASIS simulations.

Subclasses OASIS Platform with:
- Nested comments via parent_comment_id
- reply_to_comment action
- HN-only action set (no repost, follow, groups, etc.)
- Threaded comment observation for agents

Zero OASIS source changes — everything is subclassed or patched at runtime.
"""

import json
import sqlite3
from datetime import datetime
from string import Template
from typing import Any

from camel.toolkits import FunctionTool

from oasis.social_agent.agent_action import SocialAction
from oasis.social_agent.agent_environment import SocialEnvironment
from oasis.social_platform.channel import Channel
from oasis.social_platform.platform import Platform
from oasis.social_platform.typing import ActionType, RecsysType


# ── HN action names (what agents can do) ────────────────────

HN_ACTIONS = [
    "create_comment",
    "reply_to_comment",
    "like_post",
    "like_comment",
    "do_nothing",
]


# ── HNPlatform ───────────────────────────────────────────────

class HNPlatform(Platform):
    """Platform subclass with nested comment support."""

    # Custom action names not in OASIS ActionType enum
    CUSTOM_ACTIONS = {"reply_to_comment"}

    def __init__(self, db_path: str, channel: Channel, **kwargs):
        kwargs.setdefault("recsys_type", "reddit")
        kwargs.setdefault("show_score", True)
        kwargs.setdefault("allow_self_rating", True)
        kwargs.setdefault("max_rec_post_len", 100)
        kwargs.setdefault("refresh_rec_post_count", 5)
        super().__init__(db_path=db_path, channel=channel, **kwargs)
        self._add_parent_comment_column()

    async def running(self):
        """Override to handle custom actions not in ActionType enum."""
        while True:
            message_id, data = await self.channel.receive_from()
            agent_id, message, action = data

            # Handle custom actions before ActionType conversion
            if action in self.CUSTOM_ACTIONS:
                action_function = getattr(self, action, None)
                if action_function:
                    result = await action_function(agent_id, message)
                    await self.channel.send_to((message_id, agent_id, result))
                    continue

            action = ActionType(action)

            if action == ActionType.EXIT:
                if self.db_path == ":memory:":
                    dst = sqlite3.connect("mock.db")
                    with dst:
                        self.db.backup(dst)
                self.db_cursor.close()
                self.db.close()
                break

            action_function = getattr(self, action.value, None)
            if action_function:
                func_code = action_function.__code__
                param_names = func_code.co_varnames[:func_code.co_argcount]
                len_param_names = len(param_names)
                params = {}
                if len_param_names >= 2:
                    params["agent_id"] = agent_id
                if len_param_names == 3:
                    second_param_name = param_names[2]
                    params[second_param_name] = message
                result = await action_function(**params)
                await self.channel.send_to((message_id, agent_id, result))
            else:
                raise ValueError(f"Action {action} is not supported")

    def _add_parent_comment_column(self):
        """Add parent_comment_id column if it doesn't exist."""
        try:
            self.db.execute(
                "ALTER TABLE comment ADD COLUMN parent_comment_id INTEGER "
                "REFERENCES comment(comment_id)"
            )
            self.db.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    async def reply_to_comment(self, agent_id: int, reply_message: tuple):
        """Handle reply_to_comment action — nested comment."""
        parent_comment_id, content = reply_message
        current_time = self.sandbox_clock.time_transfer(
            datetime.now(), self.start_time
        )
        try:
            user_id = agent_id

            # Look up post_id from parent comment
            self.pl_utils._execute_db_command(
                "SELECT post_id FROM comment WHERE comment_id = ?",
                (parent_comment_id,),
            )
            parent = self.db_cursor.fetchone()
            if not parent:
                return {"success": False, "error": "Parent comment not found."}
            post_id = parent[0]

            # Insert comment with parent reference
            self.pl_utils._execute_db_command(
                "INSERT INTO comment (post_id, user_id, content, created_at, "
                "parent_comment_id, num_likes, num_dislikes) "
                "VALUES (?, ?, ?, ?, ?, 0, 0)",
                (post_id, user_id, content, current_time, parent_comment_id),
                commit=True,
            )
            comment_id = self.db_cursor.lastrowid

            action_info = {
                "content": content,
                "comment_id": comment_id,
                "parent_comment_id": parent_comment_id,
            }
            self.pl_utils._record_trace(
                user_id, "reply_to_comment", action_info, current_time
            )

            return {"success": True, "comment_id": comment_id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def refresh(self, agent_id: int):
        """Override refresh to render threaded comments."""
        current_time = self.sandbox_clock.time_transfer(
            datetime.now(), self.start_time
        )
        try:
            user_id = agent_id

            # Get recommended post IDs
            self.pl_utils._execute_db_command(
                "SELECT post_id FROM rec WHERE user_id = ?", (user_id,)
            )
            rec_results = self.db_cursor.fetchall()
            post_ids = [row[0] for row in rec_results]

            if not post_ids:
                self.pl_utils._record_trace(
                    user_id, ActionType.REFRESH.value, {}, current_time
                )
                return {"success": False, "message": "No posts found."}

            placeholders = ", ".join("?" for _ in post_ids)
            self.pl_utils._execute_db_command(
                f"SELECT post_id, user_id, content, created_at, "
                f"num_likes, num_dislikes "
                f"FROM post WHERE post_id IN ({placeholders}) "
                f"AND original_post_id IS NULL "
                f"ORDER BY num_likes - num_dislikes DESC",
                post_ids,
            )
            posts_raw = self.db_cursor.fetchall()

            posts = []
            for post_id, p_user_id, content, created_at, likes, dislikes in posts_raw:
                comments = self._build_comment_tree(post_id)
                posts.append({
                    "post_id": post_id,
                    "user_id": p_user_id,
                    "content": content,
                    "created_at": created_at,
                    "score": likes - dislikes,
                    "comments": comments,
                })

            action_info = {"posts": posts}
            self.pl_utils._record_trace(
                user_id, ActionType.REFRESH.value, action_info, current_time
            )
            return {"success": True, "posts": posts}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _build_comment_tree(self, post_id: int) -> list[dict]:
        """Build nested comment tree for a post."""
        self.db_cursor.execute(
            "SELECT comment_id, user_id, content, created_at, "
            "num_likes, num_dislikes, parent_comment_id "
            "FROM comment WHERE post_id = ? ORDER BY comment_id",
            (post_id,),
        )
        rows = self.db_cursor.fetchall()

        by_id: dict[int, dict] = {}
        roots: list[dict] = []

        for cid, uid, content, ts, likes, dislikes, parent_id in rows:
            node = {
                "comment_id": cid,
                "user_id": uid,
                "content": content,
                "created_at": ts,
                "score": likes - dislikes,
                "parent_comment_id": parent_id,
                "replies": [],
            }
            by_id[cid] = node

            if parent_id is None:
                roots.append(node)
            elif parent_id in by_id:
                by_id[parent_id]["replies"].append(node)
            else:
                # Parent not yet seen (shouldn't happen with ORDER BY comment_id)
                roots.append(node)

        return roots


# ── HNAction ─────────────────────────────────────────────────

class HNAction(SocialAction):
    """SocialAction subclass with reply_to_comment and HN-only tool list."""

    async def reply_to_comment(self, comment_id: int, content: str):
        """Reply to an existing comment in the discussion thread.

        Use this to respond directly to another user's comment. Your reply
        will appear nested under their comment.

        Args:
            comment_id (int): The ID of the comment you are replying to.
            content (str): Your reply text.

        Returns:
            dict: {'success': True, 'comment_id': <new_comment_id>}
        """
        message = (comment_id, content)
        return await self.perform_action(message, "reply_to_comment")

    def get_openai_function_list(self) -> list[FunctionTool]:
        """Return only HN-relevant actions as tools."""
        all_tools = super().get_openai_function_list()
        hn_set = set(HN_ACTIONS)
        return [t for t in all_tools if t.func.__name__ in hn_set] + [
            FunctionTool(self.reply_to_comment),
        ]


# ── HNEnvironment ────────────────────────────────────────────

def _render_comment_tree(
    comments: list[dict],
    depth: int = 0,
    product_user_id: int | None = None,
) -> str:
    """Render nested comments as indented text for agent observation."""
    lines = []
    for c in comments:
        indent = "  " * depth
        score_str = f" [{c['score']} points]" if c.get("score", 0) != 0 else ""
        if c['user_id'] == product_user_id:
            user_label = "[OP]"
        else:
            user_label = f"User {c['user_id']}"
        lines.append(
            f"{indent}[comment_id={c['comment_id']}] "
            f"{user_label}{score_str}: {c['content']}"
        )
        if c.get("replies"):
            lines.append(_render_comment_tree(
                c["replies"], depth + 1,
                product_user_id=product_user_id,
            ))
    return "\n".join(lines)


class HNEnvironment(SocialEnvironment):
    """Environment that renders threaded HN-style discussions."""

    posts_env_template = Template(
        "Here are the current discussions on the forum:\n\n$posts"
    )

    def __init__(self, action):
        super().__init__(action)
        self.comments_only = False
        self.product_user_id = None  # set by runner after product_team is created

    async def get_posts_env(self) -> str:
        """Override to render threaded comments."""
        posts = await self.action.refresh()
        if not posts["success"]:
            return "There are no discussions on the forum yet."

        blocks = []
        for p in posts["posts"]:
            comments = p.get("comments", [])
            render_kwargs = dict(
                product_user_id=self.product_user_id,
            )
            if self.comments_only:
                # Skip the post content, only show comments
                if comments:
                    comment_text = _render_comment_tree(comments, depth=0, **render_kwargs)
                    blocks.append(f"[Thread on post_id={p['post_id']}]\n{comment_text}")
            else:
                poster = "[OP]" if p['user_id'] == self.product_user_id else f"User {p['user_id']}"
                score_str = f" [{p['score']} points]" if p.get("score", 0) != 0 else ""
                header = (
                    f"[post_id={p['post_id']}] "
                    f"{poster}{score_str}:\n{p['content']}"
                )
                if comments:
                    comment_text = _render_comment_tree(comments, depth=1, **render_kwargs)
                    blocks.append(f"{header}\n\n  Comments:\n{comment_text}")
                else:
                    blocks.append(header)

        posts_text = "\n\n---\n\n".join(blocks)
        return self.posts_env_template.substitute(posts=posts_text)

    async def to_text_prompt(self) -> str:
        """Simplified observation — just the discussions, no follower counts."""
        posts_env = await self.get_posts_env()
        return posts_env
