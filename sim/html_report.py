"""Standalone HTML report with HN-style thread view.

Generates a self-contained HTML file with:
- Engagement sparkline (SVG)
- Verdict summary (adoption score + narrative)
- HN-style nested comment thread
All data embedded as JSON — no external dependencies.
"""

import json
import sqlite3
from pathlib import Path

from sim.synthesizer import PopulationVerdict


def generate_live_html(db: str, dest: Path) -> Path:
    """Generate a lightweight live HTML that auto-refreshes.

    No synthesis/verdict — just the thread as it builds up.
    """
    thread_data = _load_thread_data(db)
    data = {
        "verdict": None,
        "engagement": [],
        "thread": thread_data,
    }
    html = _render_html(data, live=True)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "live.html"
    out.write_text(html)
    return out


def _load_thread_data(db: str) -> dict:
    """Extract posts, comments (with nesting), and users from OASIS DB."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # Users
    users = {}
    for row in conn.execute("SELECT user_id, user_name, name, bio FROM user"):
        users[row["user_id"]] = {
            "user_id": row["user_id"],
            "name": row["user_name"] or row["name"],
            "bio": (row["bio"] or "")[:200],
        }

    # Posts (exclude reposts/quotes — just originals)
    posts = []
    for row in conn.execute(
        "SELECT post_id, user_id, content, created_at, num_likes, num_dislikes "
        "FROM post WHERE original_post_id IS NULL ORDER BY num_likes - num_dislikes DESC"
    ):
        posts.append({
            "post_id": row["post_id"],
            "user_id": row["user_id"],
            "content": row["content"],
            "created_at": row["created_at"],
            "score": row["num_likes"] - row["num_dislikes"],
        })

    # Comments with parent_comment_id
    has_parent = False
    try:
        conn.execute("SELECT parent_comment_id FROM comment LIMIT 1")
        has_parent = True
    except sqlite3.OperationalError:
        pass

    comments = []
    if has_parent:
        query = (
            "SELECT comment_id, post_id, user_id, content, created_at, "
            "num_likes, num_dislikes, parent_comment_id "
            "FROM comment ORDER BY comment_id"
        )
    else:
        query = (
            "SELECT comment_id, post_id, user_id, content, created_at, "
            "num_likes, num_dislikes "
            "FROM comment ORDER BY comment_id"
        )

    for row in conn.execute(query):
        c = {
            "comment_id": row["comment_id"],
            "post_id": row["post_id"],
            "user_id": row["user_id"],
            "content": row["content"],
            "created_at": row["created_at"],
            "score": row["num_likes"] - row["num_dislikes"],
            "parent_comment_id": row["parent_comment_id"] if has_parent else None,
        }
        comments.append(c)

    conn.close()
    return {"posts": posts, "comments": comments, "users": users}


def _load_engagement_data(db: str) -> list[dict]:
    """Load per-round engagement rates for sparkline."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            "SELECT round, "
            "SUM(CASE WHEN action_class IN ('engaged','repeated') THEN 1 ELSE 0 END) as engaged, "
            "COUNT(*) as total "
            "FROM agent_rounds GROUP BY round ORDER BY round"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    conn.close()
    return [
        {"round": r["round"], "rate": r["engaged"] / r["total"] if r["total"] else 0}
        for r in rows
    ]


def generate_html_report(
    verdict: PopulationVerdict,
    db: str,
    dest: Path,
) -> Path:
    """Generate standalone HTML report.

    Args:
        verdict: Populated PopulationVerdict.
        db: Path to OASIS SQLite DB.
        dest: Directory to write report.html into.

    Returns:
        Path to generated HTML file.
    """
    thread_data = _load_thread_data(db)
    engagement = _load_engagement_data(db)

    # Build embedded data
    report_data = {
        "verdict": {
            "adoption_score": verdict.adoption_score,
            "trajectory_summary": verdict.trajectory_summary,
            "narrative": verdict.narrative,
            "total_agents": verdict.total_agents,
            "rounds": verdict.rounds,
            "top_concerns": verdict.top_concerns,
            "agent_verdicts": [
                {
                    "user_id": v.user_id,
                    "dominant_profile": v.dominant_profile,
                    "trajectory": v.trajectory,
                    "stance": v.stance,
                    "score": round(v.score, 3),
                    "dominant_concern": v.dominant_concern,
                }
                for v in verdict.agent_verdicts
            ],
        },
        "engagement": engagement,
        "thread": thread_data,
    }

    html = _render_html(report_data)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "report.html"
    out.write_text(html)
    return out


def _render_html(data: dict, live: bool = False) -> str:
    """Render the full HTML page with embedded data."""
    data_json = json.dumps(data, indent=None, default=str)
    refresh_tag = '<meta http-equiv="refresh" content="3">' if live else ''
    is_live = 'true' if live else 'false'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_tag}
<title>Agar — Simulation Report</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: Verdana, Geneva, sans-serif; font-size: 13px; background: #f6f6ef; color: #333; }}
a {{ color: #333; text-decoration: none; }}

.container {{ max-width: 900px; margin: 0 auto; padding: 0 8px; }}

/* Header bar */
.header {{ background: #ff6600; padding: 4px 8px; margin-bottom: 16px; }}
.header h1 {{ font-size: 14px; font-weight: bold; color: #000; display: inline; }}
.header .subtitle {{ color: #fff; margin-left: 12px; font-size: 11px; }}

/* Verdict card */
.verdict {{ background: #fff; border: 1px solid #ddd; padding: 16px; margin-bottom: 16px; border-radius: 2px; }}
.verdict .score {{ font-size: 28px; font-weight: bold; color: #ff6600; }}
.verdict .score-label {{ font-size: 12px; color: #666; margin-left: 4px; }}
.verdict .narrative {{ margin-top: 10px; line-height: 1.5; color: #444; }}
.verdict .meta {{ margin-top: 8px; font-size: 11px; color: #888; }}

/* Sparkline */
.sparkline {{ margin: 12px 0; }}
.sparkline svg {{ display: block; }}
.sparkline .label {{ font-size: 10px; fill: #888; }}

/* Concerns */
.concerns {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
.concern-tag {{ background: #f0f0f0; padding: 2px 8px; border-radius: 3px; font-size: 11px; color: #555; }}

/* Thread */
.thread {{ background: #fff; border: 1px solid #ddd; padding: 0; border-radius: 2px; }}
.thread-header {{ padding: 8px 12px; background: #f8f8f0; border-bottom: 1px solid #ddd; font-size: 11px; color: #888; font-weight: bold; }}

/* Post */
.post {{ padding: 8px 12px; border-bottom: 1px solid #eee; }}
.post:last-child {{ border-bottom: none; }}
.post-header {{ font-size: 11px; color: #888; margin-bottom: 4px; }}
.post-header .user {{ color: #ff6600; font-weight: bold; }}
.post-header .score {{ color: #555; }}
.post-content {{ line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }}
.post-toggle {{ font-size: 11px; color: #888; cursor: pointer; margin-top: 4px; }}
.post-toggle:hover {{ text-decoration: underline; }}

/* Comments */
.comments {{ margin-top: 8px; }}
.comment {{ padding: 4px 0; }}
.comment-inner {{ padding: 4px 0 4px 12px; border-left: 2px solid #ddd; }}
.comment-header {{ font-size: 11px; color: #888; margin-bottom: 2px; }}
.comment-header .user {{ color: #666; font-weight: bold; }}
.comment-header .score {{ color: #555; }}
.comment-header .stance {{ font-size: 10px; padding: 1px 5px; border-radius: 3px; margin-left: 4px; }}
.stance-receptive {{ background: #e6ffe6; color: #2a7a2a; }}
.stance-skeptical {{ background: #fff3e0; color: #a06000; }}
.stance-churned {{ background: #ffe6e6; color: #a02020; }}
.stance-neutral {{ background: #f0f0f0; color: #666; }}
.comment-content {{ line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }}
.comment .comments {{ margin-left: 0; }}

/* Agent profiles in sidebar */
.agent-bar {{ font-size: 10px; color: #aaa; margin-top: 2px; }}
</style>
</head>
<body>

<div class="header">
  <h1>agar</h1>
  <span class="subtitle">simulation report</span>
</div>

<div class="container">
  <div id="verdict" class="verdict"></div>
  <div id="thread" class="thread">
    <div class="thread-header">Discussion</div>
  </div>
</div>

<script>
const DATA = {data_json};

// ── Verdict ──────────────────────────────────────────────
function renderVerdict() {{
  const v = DATA.verdict;
  const el = document.getElementById('verdict');

  // Sparkline SVG
  const eng = DATA.engagement;
  let sparkSvg = '';
  if (eng.length > 1) {{
    const w = 300, h = 40, pad = 2;
    const maxR = eng.length;
    const points = eng.map((d, i) => {{
      const x = pad + (i / (maxR - 1)) * (w - 2 * pad);
      const y = h - pad - d.rate * (h - 2 * pad);
      return x + ',' + y;
    }}).join(' ');
    const lastRate = Math.round(eng[eng.length - 1].rate * 100);
    const firstRate = Math.round(eng[0].rate * 100);
    sparkSvg = '<div class="sparkline">' +
      '<svg width="' + w + '" height="' + (h + 14) + '" xmlns="http://www.w3.org/2000/svg">' +
      '<polyline points="' + points + '" fill="none" stroke="#ff6600" stroke-width="1.5"/>' +
      '<text x="0" y="' + (h + 12) + '" class="label">R1: ' + firstRate + '%</text>' +
      '<text x="' + (w - 60) + '" y="' + (h + 12) + '" class="label">R' + maxR + ': ' + lastRate + '%</text>' +
      '</svg></div>';
  }}

  // Concerns
  const concerns = (v.top_concerns || []).slice(0, 5).map(
    c => '<span class="concern-tag">' + c[0] + ' \\u00d7' + c[1] + '</span>'
  ).join('');

  el.innerHTML =
    '<div><span class="score">' + v.adoption_score.toFixed(2) + '</span>' +
    '<span class="score-label">/ 1.0 adoption</span></div>' +
    sparkSvg +
    '<div class="narrative">' + escHtml(v.narrative) + '</div>' +
    '<div class="meta">' + v.total_agents + ' agents \\u00b7 ' + v.rounds + ' rounds \\u00b7 ' +
    v.trajectory_summary + '</div>' +
    (concerns ? '<div class="concerns">' + concerns + '</div>' : '');
}}

// ── Thread ───────────────────────────────────────────────
function renderThread() {{
  const t = DATA.thread;
  const el = document.getElementById('thread');

  // Build agent verdict lookup
  const agentMap = {{}};
  ((DATA.verdict && DATA.verdict.agent_verdicts) || []).forEach(a => {{ agentMap[a.user_id] = a; }});

  // Build user name lookup
  const userMap = t.users || {{}};

  // Group comments by post
  const commentsByPost = {{}};
  t.comments.forEach(c => {{
    if (!commentsByPost[c.post_id]) commentsByPost[c.post_id] = [];
    commentsByPost[c.post_id].push(c);
  }});

  // Render each post
  t.posts.forEach((post, idx) => {{
    const userName = userMap[post.user_id] ? userMap[post.user_id].name : 'agent_' + post.user_id;
    const agent = agentMap[post.user_id];
    const stanceHtml = agent ? stanceBadge(agent.stance) : '';

    const postEl = document.createElement('div');
    postEl.className = 'post';

    const comments = commentsByPost[post.post_id] || [];
    const commentTree = buildCommentTree(comments);
    const commentsHtml = renderComments(commentTree, userMap, agentMap);
    const commentCount = comments.length;

    const isFirst = (idx === 0);
    postEl.innerHTML =
      '<div class="post-header">' +
      '<span class="score">\\u25b2 ' + post.score + '</span> ' +
      '<span class="user">' + escHtml(userName) + '</span> ' +
      stanceHtml +
      (isFirst ? ' <span class="post-toggle" onclick="toggleContent(this)" style="cursor:pointer">[show brief]</span>' : '') +
      '</div>' +
      '<div class="post-content"' + (isFirst ? ' style="display:none"' : '') + '>' + escHtml(post.content) + '</div>' +
      (commentCount > 0 ? '<div class="post-toggle" onclick="toggleComments(this)">' +
      commentCount + ' comment' + (commentCount !== 1 ? 's' : '') + '</div>' : '') +
      '<div class="comments" style="display:block">' + commentsHtml + '</div>';

    el.appendChild(postEl);
  }});
}}

function buildCommentTree(comments) {{
  const byId = {{}};
  const roots = [];
  comments.forEach(c => {{
    c.children = [];
    byId[c.comment_id] = c;
  }});
  comments.forEach(c => {{
    if (c.parent_comment_id && byId[c.parent_comment_id]) {{
      byId[c.parent_comment_id].children.push(c);
    }} else {{
      roots.push(c);
    }}
  }});
  return roots;
}}

function renderComments(comments, userMap, agentMap) {{
  if (!comments.length) return '';
  return comments.map(c => {{
    const userName = userMap[c.user_id] ? userMap[c.user_id].name : 'agent_' + c.user_id;
    const agent = agentMap[c.user_id];
    const stanceHtml = agent ? stanceBadge(agent.stance) : '';
    const scoreStr = c.score !== 0 ? ' \\u00b7 ' + c.score + ' points' : '';
    const childrenHtml = renderComments(c.children || [], userMap, agentMap);
    return '<div class="comment"><div class="comment-inner">' +
      '<div class="comment-header"><span class="user">' + escHtml(userName) + '</span>' +
      stanceHtml + '<span class="score">' + scoreStr + '</span></div>' +
      '<div class="comment-content">' + escHtml(c.content) + '</div>' +
      (childrenHtml ? '<div class="comments">' + childrenHtml + '</div>' : '') +
      '</div></div>';
  }}).join('');
}}

function stanceBadge(stance) {{
  if (!stance) return '';
  return '<span class="stance stance-' + stance + '">' + stance + '</span>';
}}

function toggleContent(el) {{
  const content = el.parentElement.nextElementSibling;
  if (content.style.display === 'none') {{
    content.style.display = 'block';
    el.textContent = '[hide brief]';
  }} else {{
    content.style.display = 'none';
    el.textContent = '[show brief]';
  }}
}}

function toggleComments(el) {{
  const comments = el.nextElementSibling;
  if (comments.style.display === 'none') {{
    comments.style.display = 'block';
    el.textContent = el.textContent.replace('show', 'hide');
  }} else {{
    comments.style.display = 'none';
    el.textContent = el.textContent.replace('hide', 'show');
  }}
}}

function escHtml(s) {{
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

// ── Init ─────────────────────────────────────────────────
if (DATA.verdict) {{ renderVerdict(); }}
else {{ document.getElementById('verdict').style.display = 'none'; }}
renderThread();
if ({is_live}) {{
  // Restore scroll position or auto-scroll to bottom
  const saved = sessionStorage.getItem('agar_scroll');
  const wasBottom = sessionStorage.getItem('agar_at_bottom');
  setTimeout(() => {{
    if (wasBottom === 'true' || !saved) {{
      window.scrollTo(0, document.body.scrollHeight);
    }} else {{
      window.scrollTo(0, parseInt(saved) || 0);
    }}
  }}, 50);
  // Save scroll position before next refresh
  window.addEventListener('beforeunload', () => {{
    const atBottom = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 150);
    sessionStorage.setItem('agar_scroll', window.scrollY.toString());
    sessionStorage.setItem('agar_at_bottom', atBottom.toString());
  }});
}}
</script>
</body>
</html>"""
