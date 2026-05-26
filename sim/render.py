"""Render PopulationVerdict + DB evidence into a markdown report.

Pure function: takes structured data, returns a string. All DB reads
are done here (influential posts, liked comments, signal evolution)
so synthesizer.py stays data-only.
"""

import sqlite3
from pathlib import Path

from sim.synthesizer import PopulationVerdict


def _load_influential_posts(db: str, n: int = 5) -> list[dict]:
    """Load posts with most replies (influence proxy), excluding injected brief."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    max_agent = conn.execute("SELECT MAX(user_id) FROM user").fetchone()[0]
    rows = conn.execute("""
        SELECT p.post_id, p.user_id, p.content,
               (SELECT COUNT(*) FROM comment c WHERE c.post_id = p.post_id) AS replies
        FROM post p
        WHERE p.user_id != ?
        ORDER BY replies DESC
        LIMIT ?
    """, (max_agent, n)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_liked_comments(db: str, n: int = 5) -> list[dict]:
    """Load comments that got likes (social proof signal)."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.comment_id, c.user_id, c.content, c.num_likes,
               ct.tag, ct.score
        FROM comment c
        JOIN comment_tags ct ON ct.comment_id = c.comment_id
        WHERE c.num_likes > 0
          AND ct.score = (SELECT MAX(ct2.score) FROM comment_tags ct2
                          WHERE ct2.comment_id = c.comment_id)
        ORDER BY c.num_likes DESC, ct.score DESC
        LIMIT ?
    """, (n,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_top_comments(db: str, n: int = 5) -> list[dict]:
    """Load top N comments by friction score for narrative context."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.user_id, c.content, ct.tag, ct.score
        FROM comment c
        JOIN comment_tags ct ON ct.comment_id = c.comment_id
        WHERE ct.score = (
            SELECT MAX(ct2.score) FROM comment_tags ct2
            WHERE ct2.comment_id = c.comment_id
        )
        ORDER BY ct.score DESC
        LIMIT ?
    """, (n,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _render_signal_evolution(db: str) -> str:
    """Render round-by-round friction signal as a compact table."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT round, tag, agent_count FROM round_signals ORDER BY round, agent_count DESC"
    ).fetchall()
    conn.close()

    by_round: dict[int, list[tuple[str, int]]] = {}
    for r in rows:
        by_round.setdefault(r["round"], []).append((r["tag"], r["agent_count"]))

    lines = []
    for rnd in sorted(by_round):
        tags = ", ".join(f"{tag}×{cnt}" for tag, cnt in by_round[rnd][:3])
        lines.append(f"| {rnd} | {tags} |")

    header = "| Round | Dominant signals |\n|-------|-----------------|"
    return header + "\n" + "\n".join(lines)


def render_report(verdict: PopulationVerdict, db: str) -> str:
    """Render a PopulationVerdict + DB evidence into a markdown report.

    Args:
        verdict: Fully populated PopulationVerdict (with narrative).
        db: Path to OASIS SQLite DB for evidence queries.

    Returns:
        Markdown string.
    """
    stances: dict[str, int] = {}
    for v in verdict.agent_verdicts:
        stances[v.stance] = stances.get(v.stance, 0) + 1
    stance_line = ", ".join(
        f"{cnt} {s}" for s, cnt in sorted(stances.items(), key=lambda x: x[1], reverse=True)
    )

    signal_table = _render_signal_evolution(db)

    concerns = "\n".join(
        f"| {tag} | {cnt} |" for tag, cnt in verdict.top_concerns[:5]
    )

    seg_rows = []
    for tag, s in sorted(verdict.segments.items(), key=lambda x: x[1]["total"], reverse=True):
        if s["total"] == 0:
            continue
        seg_rows.append(
            f"| {tag} | {s['receptive']} | {s['skeptical']} | {s['churned']} | {s['neutral']} | {s['total']} |"
        )
    segments = "\n".join(seg_rows)

    traj_rows = []
    for v in sorted(verdict.agent_verdicts, key=lambda x: x.score, reverse=True):
        traj_rows.append(
            f"| {v.user_id} | {v.dominant_profile} | {v.trajectory} | "
            f"{v.stance} | {v.score:.2f} | {v.dominant_concern or '-'} |"
        )
    trajectories = "\n".join(traj_rows)

    inf_posts = _load_influential_posts(db)
    post_blocks = []
    for p in inf_posts[:3]:
        if p["replies"] == 0:
            continue
        content = p["content"][:200].replace("\n", " ")
        post_blocks.append(
            f'**Post #{p["post_id"]}** (agent {p["user_id"]}) — '
            f'{p["replies"]} replies\n> {content}...'
        )
    posts_section = "\n\n".join(post_blocks) if post_blocks else "(no threaded discussion)"

    liked = _load_liked_comments(db)
    liked_blocks = []
    for c in liked[:3]:
        content = c["content"][:200].replace("\n", " ")
        liked_blocks.append(
            f'**[{c["tag"]}]** (agent {c["user_id"]}, {c["num_likes"]} likes)\n> {content}...'
        )
    liked_section = "\n\n".join(liked_blocks) if liked_blocks else "(no liked comments)"

    top_comments = _load_top_comments(db, n=3)
    top_blocks = []
    for c in top_comments:
        content = c["content"][:200].replace("\n", " ")
        top_blocks.append(
            f'**[{c["tag"]}]** (agent {c["user_id"]})\n> {content}...'
        )
    top_section = "\n\n".join(top_blocks) if top_blocks else "(no comments)"

    return f"""# Agar Simulation Report

## Verdict

**Adoption score: {verdict.adoption_score:.2f} / 1.0** — {stance_line}

{verdict.trajectory_summary}

{verdict.narrative}

---

## Friction Signals

### Top Concerns

| Concern | Agents |
|---------|--------|
{concerns}

### Signal Evolution (per round)

{signal_table}

---

## Conversation Evidence

### Posts That Shaped the Discussion

{posts_section}

### Comments With Social Proof (liked by other agents)

{liked_section}

### Highest Friction Comments

{top_section}

---

## Agent Trajectories

| Agent | Profile | Trajectory | Stance | Score | Concern |
|-------|---------|------------|--------|-------|---------|
{trajectories}

Trajectory key: e=engaged, r=repeated, p=passive, s=silent

## Segment Breakdown

| Segment | Receptive | Skeptical | Churned | Neutral | Total |
|---------|-----------|-----------|---------|---------|-------|
{segments}
"""
