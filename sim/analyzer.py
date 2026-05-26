"""Per-round agent classification and population aggregation.

Reads from the OASIS trace/comment tables and comment_tags (from tagger.py)
to produce:
  - agent_rounds: per-agent per-round action classification
  - round_signals: population-level friction signal per round

Round inference: OASIS doesn't store round numbers in the trace. Each round
produces one `refresh` per agent followed by one action. We rank refreshes
per agent chronologically to assign round numbers, then attribute each
comment to the round in which it was created (between two consecutive refreshes).

Runs after tagger.py each round. Pure SQL/Python — no LLM calls.
"""

import sqlite3


ENGAGED = "engaged"
REPEATED = "repeated"
PASSIVE = "passive"
SILENT = "silent"

COMMENT_ACTIONS = {"create_comment", "create_post", "quote_post", "reply_to_comment"}
PASSIVE_ACTIONS = {"like_post", "dislike_post", "like_comment", "dislike_comment"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_rounds (
            user_id         INTEGER NOT NULL,
            round           INTEGER NOT NULL,
            action_class    TEXT NOT NULL,
            dominant_tag    TEXT,
            PRIMARY KEY (user_id, round)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS round_signals (
            round               INTEGER NOT NULL,
            tag                 TEXT NOT NULL,
            comment_count       INTEGER NOT NULL DEFAULT 0,
            agent_count         INTEGER NOT NULL DEFAULT 0,
            engagement_rate     REAL NOT NULL DEFAULT 0,
            do_nothing_rate     REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (round, tag)
        )
    """)
    conn.commit()


def _build_agent_round_index(conn: sqlite3.Connection) -> dict[int, list[tuple]]:
    """Build per-agent round timeline: {user_id: [(round, refresh_ts, next_refresh_ts), ...]}.

    Each round spans from the refresh timestamp up to (but not including)
    the next refresh, or infinity for the last round.
    """
    rows = conn.execute("""
        SELECT user_id, created_at
        FROM trace
        WHERE action = 'refresh'
          AND user_id NOT IN (SELECT user_id FROM trace WHERE action = 'sign_up'
                              EXCEPT SELECT user_id FROM trace WHERE action = 'refresh')
        ORDER BY user_id, created_at
    """).fetchall()

    index: dict[int, list[tuple]] = {}
    for user_id, ts in rows:
        index.setdefault(user_id, []).append(ts)

    result: dict[int, list[tuple]] = {}
    for user_id, refreshes in index.items():
        rounds = []
        for i, ts in enumerate(refreshes):
            next_ts = refreshes[i + 1] if i + 1 < len(refreshes) else "9999-99-99"
            rounds.append((i + 1, ts, next_ts))
        result[user_id] = rounds
    return result


def _comment_round(
    conn: sqlite3.Connection,
    user_id: int,
    round_index: dict[int, list[tuple]],
) -> dict[int, int]:
    """Map comment_id → round_num for a given agent.

    A comment belongs to round N if its created_at falls between
    round N's refresh and the next refresh.
    """
    comments = conn.execute(
        "SELECT comment_id, created_at FROM comment WHERE user_id = ?", (user_id,)
    ).fetchall()
    rounds = round_index.get(user_id, [])

    result: dict[int, int] = {}
    for comment_id, comment_ts in comments:
        for round_num, refresh_ts, next_ts in rounds:
            if refresh_ts <= comment_ts < next_ts:
                result[comment_id] = round_num
                break
    return result


def classify_round(db: str, round_num: int, total_agents: int) -> dict:
    """Classify all agents for a given round and update round_signals.

    Args:
        db: Path to OASIS SQLite DB.
        round_num: 1-indexed round number.
        total_agents: Total number of sim agents (for rate computation).

    Returns:
        Dict with round summary: counts per action_class, dominant tags.
    """
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    already = {
        r[0] for r in conn.execute(
            "SELECT user_id FROM agent_rounds WHERE round = ?", (round_num,)
        ).fetchall()
    }

    round_index = _build_agent_round_index(conn)

    # Agents that have a round N in the index
    eligible = [uid for uid, rounds in round_index.items() if any(r[0] == round_num for r in rounds)]
    to_classify = [uid for uid in eligible if uid not in already]

    if not to_classify:
        conn.close()
        return {}

    # All trace actions per agent this round
    classified = []
    for user_id in to_classify:
        rounds = round_index[user_id]
        round_entry = next((r for r in rounds if r[0] == round_num), None)
        if not round_entry:
            continue
        _, refresh_ts, next_ts = round_entry

        actions = conn.execute("""
            SELECT action FROM trace
            WHERE user_id = ?
              AND created_at > ?
              AND created_at < ?
              AND action NOT IN ('sign_up', 'update_rec', 'refresh')
        """, (user_id, refresh_ts, next_ts)).fetchall()
        action_set = {r["action"] for r in actions}

        # Dominant tag: comments in this round window
        comment_map = _comment_round(conn, user_id, round_index)
        this_round_comments = [cid for cid, rnd in comment_map.items() if rnd == round_num]

        dominant = None
        if this_round_comments:
            placeholders = ",".join("?" * len(this_round_comments))
            row = conn.execute(
                f"SELECT tag FROM comment_tags WHERE comment_id IN ({placeholders}) "
                f"ORDER BY score DESC LIMIT 1",
                this_round_comments,
            ).fetchone()
            if row:
                dominant = row["tag"]

        prior = conn.execute(
            "SELECT dominant_tag FROM agent_rounds WHERE user_id = ? ORDER BY round DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        prior_tag = prior["dominant_tag"] if prior else None

        if action_set & COMMENT_ACTIONS:
            action_class = REPEATED if dominant and dominant == prior_tag else ENGAGED
        elif action_set & PASSIVE_ACTIONS:
            action_class = PASSIVE
        else:
            action_class = SILENT

        classified.append((user_id, round_num, action_class, dominant))

    conn.executemany(
        "INSERT OR IGNORE INTO agent_rounds (user_id, round, action_class, dominant_tag) "
        "VALUES (?, ?, ?, ?)",
        classified,
    )
    conn.commit()

    # Aggregate round_signals per dominant tag
    tag_rows = conn.execute("""
        SELECT dominant_tag AS tag,
               COUNT(*) AS agent_count,
               SUM(CASE WHEN action_class IN ('engaged','repeated') THEN 1 ELSE 0 END) AS engaged,
               SUM(CASE WHEN action_class = 'silent' THEN 1 ELSE 0 END) AS silent_count
        FROM agent_rounds
        WHERE round = ? AND dominant_tag IS NOT NULL
        GROUP BY dominant_tag
    """, (round_num,)).fetchall()

    for row in tag_rows:
        conn.execute(
            "INSERT OR REPLACE INTO round_signals "
            "(round, tag, comment_count, agent_count, engagement_rate, do_nothing_rate) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                round_num, row["tag"],
                row["agent_count"],
                row["agent_count"],
                row["engaged"] / total_agents if total_agents else 0,
                row["silent_count"] / total_agents if total_agents else 0,
            ),
        )
    conn.commit()

    counts: dict[str, int] = {ENGAGED: 0, REPEATED: 0, PASSIVE: 0, SILENT: 0}
    tag_counts: dict[str, int] = {}
    for _, _, action_class, dominant in classified:
        counts[action_class] = counts.get(action_class, 0) + 1
        if dominant:
            tag_counts[dominant] = tag_counts.get(dominant, 0) + 1

    # Agents with no trace this round are silent
    silent_gap = total_agents - len(classified)
    if silent_gap > 0:
        counts[SILENT] = counts.get(SILENT, 0) + silent_gap

    conn.close()
    return {
        "round": round_num,
        "total_agents": total_agents,
        "classified": len(classified),
        "counts": counts,
        "dominant_tags": sorted(tag_counts.items(), key=lambda x: x[1], reverse=True),
    }


def format_highlight(summary: dict) -> str:
    """Format round summary as a one-line terminal highlight."""
    if not summary:
        return "(no new activity)"
    counts = summary["counts"]
    n = summary["total_agents"]
    engaged_pct = round(100 * (counts.get(ENGAGED, 0) + counts.get(REPEATED, 0)) / n) if n else 0
    silent_pct = round(100 * counts.get(SILENT, 0) / n) if n else 0
    repeated_pct = round(100 * counts.get(REPEATED, 0) / n) if n else 0
    top_tags = ", ".join(
        f"{tag}×{cnt}" for tag, cnt in summary["dominant_tags"][:3]
    )
    return (
        f"Round {summary['round']}: "
        f"{engaged_pct}% engaged ({repeated_pct}% repeated), "
        f"{silent_pct}% silent"
        + (f" | {top_tags}" if top_tags else "")
    )
