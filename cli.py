"""Agar CLI — subcommands for simulation lifecycle.

Usage:
    uv run python cli.py new    --data scored.jsonl --brief partiful.md
    uv run python cli.py run    sim_xxx --rounds 5
    uv run python cli.py run    sim_xxx --until-converge
    uv run python cli.py tag    sim_xxx baseline
    uv run python cli.py inject sim_xxx --file cut-social-features.md
    uv run python cli.py revert sim_xxx baseline
    uv run python cli.py report sim_xxx
    uv run python cli.py status sim_xxx
    uv run python cli.py ls
"""

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

from sim.session import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

_prompt_logger = logging.getLogger("sim.prompts")
_prompt_logger.setLevel(logging.DEBUG)
_prompt_logger.propagate = False


def _setup_prompt_log(session_dir: Path) -> logging.FileHandler:
    handler = logging.FileHandler(session_dir / "prompts.log")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s\n%(message)s\n" + "=" * 80,
        datefmt="%H:%M:%S",
    ))
    _prompt_logger.addHandler(handler)
    return handler


DEFAULT_BRIEF = """\
BudgetBuddy — A budgeting app built for freelancers.

Core features:
- Automatic invoice tracking: connects to your email, detects invoices, \
extracts amounts and due dates
- Tax estimation: estimates quarterly taxes based on your income and deductions
- Expense categorization: auto-tags expenses as business vs personal
- Free tier: up to 10 invoices/month, basic tax estimation
- Pro tier ($12/month): unlimited invoices, advanced tax scenarios, \
multi-currency support

Just launched. Looking for feedback from freelancers who manage their own \
finances."""


# ── Subcommands ──────────────────────────────────────────────


def cmd_setup(args: argparse.Namespace) -> None:
    from sim.setup import generate_personas, save_personas
    personas = generate_personas(args.manifest, seed=args.seed)
    out = save_personas(personas, args.out)
    roles = {}
    for p in personas:
        roles[p["role"]] = roles.get(p["role"], 0) + 1
    print(f"Generated {len(personas)} personas → {out}")
    for role, n in sorted(roles.items(), key=lambda x: -x[1]):
        print(f"  {role}: {n}")


async def cmd_new(args: argparse.Namespace) -> None:
    if args.brief:
        brief = Path(args.brief).read_text()
    else:
        brief = DEFAULT_BRIEF

    session = Session.create(
        data_path=str(args.data),
        brief=brief,
        population=args.population,
        seed=args.seed,
        model=args.model,
        timeout=args.timeout,
        activation=args.activation,
    )
    print(f"Created session: {session.session_id}")


async def cmd_run(args: argparse.Namespace) -> None:
    session = Session.load(args.session)
    handler = _setup_prompt_log(session.session_dir)

    rounds = args.rounds
    until_converge = args.until_converge

    if until_converge and rounds is None:
        rounds = 50  # safety cap

    live = args.live
    if live:
        live_dir = session.session_dir / "reports" / "live"
        live_dir.mkdir(parents=True, exist_ok=True)
        live_path = live_dir / "live.html"
        live_path.write_text(
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="2">'
            '<title>agar — waiting for sim...</title></head>'
            '<body style="font-family:monospace;padding:40px;color:#888">'
            'Waiting for simulation to start...</body></html>'
        )
        print(f"Live view: {live_path.resolve()}")
        subprocess.Popen(["open", str(live_path.resolve())])

    executed = await session.run(rounds=rounds, until_converge=until_converge, live=live)

    status = session.status()
    print(f"Ran {executed} rounds (total: {status['current_round']}, status: {status['status']})")

    if status["status"] == "converged":
        print(f"Auto-tagged: baseline")

    await session.close()
    handler.close()
    _prompt_logger.removeHandler(handler)


async def cmd_tag(args: argparse.Namespace) -> None:
    session = Session.load(args.session)
    await session.tag(args.name)
    print(f"Tagged: {args.name}")
    await session.close()


async def cmd_inject(args: argparse.Namespace) -> None:
    session = Session.load(args.session)
    handler = _setup_prompt_log(session.session_dir)

    if args.file:
        content = Path(args.file).read_text()
    else:
        content = args.content

    if not content:
        print("Error: provide content or --file", file=sys.stderr)
        sys.exit(1)

    await session.inject(content)
    print(f"Injected into {args.session}")

    await session.close()
    handler.close()
    _prompt_logger.removeHandler(handler)


async def cmd_revert(args: argparse.Namespace) -> None:
    session = Session.load(args.session)
    await session.revert(args.tag)
    print(f"Reverted to: {args.tag}")


async def cmd_report(args: argparse.Namespace) -> None:
    session = Session.load(args.session)
    report_path = await session.report(tag_name=args.tag)
    print(f"Report: {report_path}")
    if report_path.suffix == ".html":
        subprocess.Popen(["open", str(report_path.resolve())])
    await session.close()


async def cmd_status(args: argparse.Namespace) -> None:
    session = Session.load(args.session)
    s = session.status()
    print(f"Session:  {s['session_id']}")
    print(f"Status:   {s['status']}")
    print(f"Pop:      {s['population']}")
    print(f"Round:    {s['current_round']}")
    print(f"Model:    {s['model']}")
    print(f"Tags:     {', '.join(s['tags']) or '(none)'}")
    print(f"On disk:  {', '.join(s['available_tags']) or '(none)'}")
    print(f"Created:  {s['created_at']}")


async def cmd_fork(args: argparse.Namespace) -> None:
    session = Session.fork(args.session, args.tag, name=args.name)
    print(f"Forked: {session.session_id} (from {args.session}:{args.tag})")


async def cmd_ls(args: argparse.Namespace) -> None:
    sessions = Session.list_sessions()
    if not sessions:
        print("No sessions found.")
        return
    for s in sessions:
        tags = ", ".join(s["tags"]) if s["tags"] else ""
        print(f"  {s['session_id']}  pop={s['population']}  round={s['current_round']}  "
              f"status={s['status']}  tags=[{tags}]")


# ── Parser ───────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agar",
        description="Agar — social simulation CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="Generate personas from source artifacts (reviews + forum voice)")
    p_setup.add_argument("manifest", type=Path, help="YAML manifest file")
    p_setup.add_argument("--out", type=Path, default=Path("data/personas.jsonl"),
                         help="Output personas file (default: data/personas.jsonl)")
    p_setup.add_argument("--seed", type=int, default=42)

    # new
    p_new = sub.add_parser("new", help="Create a new simulation session")
    p_new.add_argument("--data", type=Path, required=True, help="Path to scored JSONL")
    p_new.add_argument("--brief", type=str, default=None, help="Path to product brief")
    p_new.add_argument("--population", type=int, default=10)
    p_new.add_argument("--seed", type=int, default=42)
    p_new.add_argument("--model", default="haiku")
    p_new.add_argument("--timeout", type=float, default=60.0)
    p_new.add_argument("--activation", type=float, default=0.2,
                       help="Ratio of agents active per round (0.0-1.0, default 0.2)")

    # run
    p_run = sub.add_parser("run", help="Run simulation rounds")
    p_run.add_argument("session", help="Session ID")
    p_run.add_argument("--rounds", type=int, default=None)
    p_run.add_argument("--until-converge", action="store_true")
    p_run.add_argument("--live", action="store_true", help="Open live thread view in browser (sequential mode)")

    # tag
    p_tag = sub.add_parser("tag", help="Tag current state")
    p_tag.add_argument("session", help="Session ID")
    p_tag.add_argument("name", help="Tag name")

    # inject
    p_inject = sub.add_parser("inject", help="Inject a product change")
    p_inject.add_argument("session", help="Session ID")
    p_inject.add_argument("content", nargs="?", default=None, help="Change description")
    p_inject.add_argument("--file", type=str, default=None, help="File with change description")

    # fork
    p_fork = sub.add_parser("fork", help="Fork a new session from a tagged state")
    p_fork.add_argument("session", help="Source session ID")
    p_fork.add_argument("tag", help="Tag to fork from")
    p_fork.add_argument("--name", type=str, default=None, help="Suffix for forked session ID")

    # revert
    p_revert = sub.add_parser("revert", help="Revert to a tagged state")
    p_revert.add_argument("session", help="Session ID")
    p_revert.add_argument("tag", help="Tag name to revert to")

    # report
    p_report = sub.add_parser("report", help="Generate synthesis report")
    p_report.add_argument("session", help="Session ID")
    p_report.add_argument("--tag", type=str, default=None, help="Tag to report on")

    # status
    p_status = sub.add_parser("status", help="Show session status")
    p_status.add_argument("session", help="Session ID")

    # ls
    sub.add_parser("ls", help="List all sessions")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "new": cmd_new,
        "run": cmd_run,
        "tag": cmd_tag,
        "inject": cmd_inject,
        "fork": cmd_fork,
        "revert": cmd_revert,
        "report": cmd_report,
        "status": cmd_status,
        "ls": cmd_ls,
    }

    if args.command == "setup":
        cmd_setup(args)
    else:
        asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()
