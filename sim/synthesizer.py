"""Synthesis layer — per-agent trajectory collapse and population verdict.

Two passes:
  1. Per-agent: collapse N round summaries into a verdict (LLM, tiny input).
  2. Population: aggregate verdicts + one LLM narrative call.

Reads from agent_rounds, round_signals, comment_tags — populated by
analyzer.py and tagger.py. Never reads raw comments at population scale.

Output: PopulationVerdict data structure + synthesis.json via save_synthesis().
Markdown rendering is in render.py.
"""

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path


# ── Agent verdict ────────────────────────────────────────────

@dataclass
class AgentVerdict:
    user_id: int
    dominant_profile: str        # dominant friction tag from scored profile
    trajectory: str              # engaged→silent, engaged→repeated→silent, etc.
    stance: str                  # receptive | skeptical | churned | neutral
    dominant_concern: str | None # top friction tag expressed in comments
    score: float                 # 0=churned, 1=receptive
    rounds: int


@dataclass
class PopulationVerdict:
    total_agents: int
    rounds: int
    adoption_score: float        # population-weighted mean agent score
    top_concerns: list[tuple[str, int]]  # [(tag, agent_count), ...]
    segments: dict[str, dict]    # {friction_tag: {receptive, skeptical, churned, total}}
    trajectory_summary: str      # "engagement decayed from 100% → 10% over 5 rounds"
    narrative: str               # LLM-generated one-paragraph synthesis
    agent_verdicts: list[AgentVerdict]


# ── DB queries ───────────────────────────────────────────────

def _load_agent_trajectories(db: str) -> dict[int, list[dict]]:
    """Load per-agent round summaries from agent_rounds."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT user_id, round, action_class, dominant_tag "
        "FROM agent_rounds ORDER BY user_id, round"
    ).fetchall()
    conn.close()

    result: dict[int, list[dict]] = {}
    for r in rows:
        result.setdefault(r["user_id"], []).append({
            "round": r["round"],
            "action_class": r["action_class"],
            "dominant_tag": r["dominant_tag"],
        })
    return result


def _load_round_signals(db: str) -> list[dict]:
    """Load population-level round signals."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT round, tag, comment_count, agent_count, engagement_rate, do_nothing_rate "
        "FROM round_signals ORDER BY round, engagement_rate DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_top_comments(db: str, n: int = 5) -> list[dict]:
    """Load top N comments by friction score (for narrative prompt context)."""
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


# ── Per-agent collapse ───────────────────────────────────────

def _trajectory_string(rounds: list[dict]) -> str:
    """Compact trajectory: 'engaged→silent→passive→silent→silent'."""
    return "→".join(r["action_class"][0] for r in rounds)  # e → s → p etc.


def _score_from_trajectory(rounds: list[dict]) -> float:
    """Heuristic score 0-1 from action class sequence.

    Weights later rounds more (recency bias — final state matters more).
    engaged=1.0, repeated=0.5, passive=0.3, silent=0.0
    """
    weights = {"engaged": 1.0, "repeated": 0.5, "passive": 0.3, "silent": 0.0}
    n = len(rounds)
    if n == 0:
        return 0.0
    # Linear recency weighting
    total_weight = sum(i + 1 for i in range(n))
    score = sum(
        weights.get(r["action_class"], 0.0) * (i + 1)
        for i, r in enumerate(rounds)
    )
    return score / total_weight if total_weight else 0.0


def _stance_from_score(score: float, trajectory: str) -> str:
    if score >= 0.6:
        return "receptive"
    if score <= 0.15:
        return "churned"
    # Agents who engaged then went silent vs never engaged
    if "e" in trajectory and trajectory.endswith("s"):
        return "skeptical"
    return "neutral"


def _collapse_agent(
    user_id: int,
    rounds: list[dict],
    dominant_profile: str,
) -> AgentVerdict:
    """Collapse per-round data into a single agent verdict (no LLM)."""
    traj = _trajectory_string(rounds)
    score = _score_from_trajectory(rounds)
    stance = _stance_from_score(score, traj)

    # Dominant concern: most frequent non-None tag across rounds
    tag_counts: dict[str, int] = {}
    for r in rounds:
        if r["dominant_tag"]:
            tag_counts[r["dominant_tag"]] = tag_counts.get(r["dominant_tag"], 0) + 1
    dominant_concern = max(tag_counts, key=lambda t: tag_counts[t]) if tag_counts else None

    return AgentVerdict(
        user_id=user_id,
        dominant_profile=dominant_profile,
        trajectory=traj,
        stance=stance,
        dominant_concern=dominant_concern,
        score=score,
        rounds=len(rounds),
    )


# ── Population aggregation ───────────────────────────────────

def _aggregate_verdicts(
    verdicts: list[AgentVerdict],
    round_signals: list[dict],
    total_agents: int,
    total_rounds: int,
) -> PopulationVerdict:
    """Aggregate agent verdicts into population-level stats (no LLM)."""
    adoption_score = sum(v.score for v in verdicts) / len(verdicts) if verdicts else 0.0

    # Top concerns by agent count
    concern_counts: dict[str, int] = {}
    for v in verdicts:
        if v.dominant_concern:
            concern_counts[v.dominant_concern] = concern_counts.get(v.dominant_concern, 0) + 1
    top_concerns = sorted(concern_counts.items(), key=lambda x: x[1], reverse=True)

    # Segments: per dominant_profile → stance breakdown
    segments: dict[str, dict] = {}
    for v in verdicts:
        seg = segments.setdefault(v.dominant_profile, {
            "receptive": 0, "skeptical": 0, "churned": 0, "neutral": 0, "total": 0,
        })
        seg[v.stance] += 1
        seg["total"] += 1

    # Trajectory summary from agent verdicts by round
    round_engagement: dict[int, int] = {}
    round_totals: dict[int, int] = {}
    for v in verdicts:
        for i, ch in enumerate(v.trajectory.split("→")):
            rnd = i + 1
            round_totals[rnd] = round_totals.get(rnd, 0) + 1
            if ch in ("e", "r"):
                round_engagement[rnd] = round_engagement.get(rnd, 0) + 1
    if round_totals:
        first_rnd = min(round_totals)
        last_rnd = max(round_totals)
        r1_pct = round(100 * round_engagement.get(first_rnd, 0) / round_totals[first_rnd])
        rn_pct = round(100 * round_engagement.get(last_rnd, 0) / round_totals[last_rnd])
        trajectory_summary = (
            f"Engagement: {r1_pct}% (round 1) → {rn_pct}% (round {last_rnd}) "
            f"over {total_rounds} rounds"
        )
    else:
        trajectory_summary = f"No signal data over {total_rounds} rounds"

    return PopulationVerdict(
        total_agents=total_agents,
        rounds=total_rounds,
        adoption_score=round(adoption_score, 3),
        top_concerns=top_concerns,
        segments=segments,
        trajectory_summary=trajectory_summary,
        narrative="",  # filled by LLM call below
        agent_verdicts=verdicts,
    )


# ── LLM narrative ────────────────────────────────────────────

def _build_narrative_prompt(verdict: PopulationVerdict, top_comments: list[dict]) -> str:
    concern_lines = "\n".join(
        f"  - {tag}: {cnt} agents" for tag, cnt in verdict.top_concerns[:5]
    )
    stance_lines = "\n".join(
        f"  - {tag}: {s['receptive']}R / {s['skeptical']}S / {s['churned']}C / {s['neutral']}N "
        f"(of {s['total']})"
        for tag, s in sorted(verdict.segments.items(), key=lambda x: x[1]["total"], reverse=True)
    )
    comment_lines = "\n".join(
        f'  [{c["tag"]}] "{c["content"][:120]}..."'
        for c in top_comments[:3]
    )
    return f"""\
You are analyzing a product simulation. A population of {verdict.total_agents} behaviorally
grounded users encountered a product brief and discussed it over {verdict.rounds} rounds.

Quantitative summary:
- Adoption score: {verdict.adoption_score:.2f} / 1.0 (0=churned, 1=receptive)
- {verdict.trajectory_summary}

Top friction concerns (by agent count):
{concern_lines}

Segment breakdown (R=receptive, S=skeptical, C=churned, N=neutral):
{stance_lines}

Representative comments (highest friction signal):
{comment_lines}

Write exactly 2-3 sentences. State the population's reaction, the single biggest
blocker, and what would move the needle. Be specific and direct. No hedging.
Do not repeat the numbers — synthesize them into a judgment.
Start directly with the verdict — no preamble, no "here is my analysis" framing."""


def _call_llm(prompt: str, model: str = "haiku", timeout: float = 30.0) -> str:
    """Call claude CLI for narrative generation."""
    result = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json",
         "--no-session-persistence", "--allowedTools", ""],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        return "(narrative unavailable)"
    try:
        return json.loads(result.stdout).get("result", "(narrative unavailable)")
    except (json.JSONDecodeError, KeyError):
        return "(narrative unavailable)"


# ── Public API ───────────────────────────────────────────────

def synthesize(
    db: str,
    agent_profiles: list[dict],
    total_rounds: int,
    model: str = "haiku",
    llm_timeout: float = 30.0,
) -> PopulationVerdict:
    """Run full synthesis: per-agent collapse + population verdict + LLM narrative.

    Args:
        db: Path to OASIS SQLite DB (must have agent_rounds, round_signals, comment_tags).
        agent_profiles: Sampled profile dicts (for dominant friction tag lookup).
        total_rounds: Number of rounds run.
        model: Claude model for narrative generation.
        llm_timeout: Timeout for LLM call in seconds.

    Returns:
        PopulationVerdict with all fields populated.
    """
    profile_map = {p["uid"]: p.get("dominant", "unknown") for p in agent_profiles}

    trajectories = _load_agent_trajectories(db)
    round_signals = _load_round_signals(db)
    top_comments = _load_top_comments(db)

    # Per-agent collapse (no LLM)
    verdicts: list[AgentVerdict] = []
    for user_id, rounds in trajectories.items():
        # OASIS user_id is index into profiles list
        uid_key = agent_profiles[user_id]["uid"] if user_id < len(agent_profiles) else str(user_id)
        dominant_profile = profile_map.get(uid_key, "unknown")
        verdicts.append(_collapse_agent(user_id, rounds, dominant_profile))

    # Population aggregation (no LLM)
    verdict = _aggregate_verdicts(
        verdicts, round_signals, len(agent_profiles), total_rounds
    )

    # One LLM call for narrative
    prompt = _build_narrative_prompt(verdict, top_comments)
    verdict.narrative = _call_llm(prompt, model=model, timeout=llm_timeout)

    return verdict


def save_synthesis(verdict: PopulationVerdict, dest: Path) -> None:
    """Write synthesis.json to dest directory."""
    data = {
        "adoption_score": verdict.adoption_score,
        "trajectory_summary": verdict.trajectory_summary,
        "top_concerns": verdict.top_concerns,
        "segments": verdict.segments,
        "narrative": verdict.narrative,
        "agent_verdicts": [
            {
                "user_id": v.user_id,
                "dominant_profile": v.dominant_profile,
                "trajectory": v.trajectory,
                "stance": v.stance,
                "dominant_concern": v.dominant_concern,
                "score": round(v.score, 3),
            }
            for v in verdict.agent_verdicts
        ],
    }
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "synthesis.json").write_text(json.dumps(data, indent=2))
