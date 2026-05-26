"""Session lifecycle — create, load, run, inject, tag, revert, report.

A session owns a simulation's config, profiles, brief, and state directory.
All persistence is in data/sessions/{session_id}/.
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from sim.claude_model import ClaudeCliModel
from sim.html_report import generate_html_report, generate_live_html
from sim.render import render_report
from sim.runner import (
    SimConfig, SimState,
    create_simulation, resume_simulation,
    inject_post, advance, tag_state, revert_state, close,
)
from sim.sampler import load_profiles, sample_population
from sim.state import db_path, list_tags
from sim.synthesizer import synthesize, save_synthesis

log = logging.getLogger("agar.session")

SESSIONS_DIR = Path("data/sessions")


class Session:
    """Owns a simulation's lifecycle and persistence."""

    def __init__(self, session_id: str, config: dict, session_dir: Path):
        self.session_id = session_id
        self.config = config
        self.session_dir = session_dir
        self._state: SimState | None = None
        self._model_backend = None  # set externally to override ClaudeCliModel

    @property
    def sim_base(self) -> str:
        return str(self.session_dir / "sim")

    @property
    def live_db(self) -> str:
        return str(db_path(self.sim_base, self.session_id))

    # ── Persistence ──────────────────────────────────────────

    def _save_config(self) -> None:
        (self.session_dir / "session.json").write_text(
            json.dumps(self.config, indent=2)
        )

    def _load_profiles(self) -> list[dict]:
        profiles = []
        with open(self.session_dir / "profiles_full.jsonl") as f:
            for line in f:
                profiles.append(json.loads(line))
        return profiles

    def _load_brief(self) -> str:
        return (self.session_dir / "brief.txt").read_text()

    # ── Factory methods ──────────────────────────────────────

    @staticmethod
    def create(
        data_path: str,
        brief: str,
        population: int = 10,
        seed: int = 42,
        model: str = "haiku",
        timeout: float = 60.0,
        activation: float = 0.2,
    ) -> "Session":
        """Create a new session: sample profiles, persist config + data."""
        session_id = f"sim_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        session_dir = SESSIONS_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        all_profiles = load_profiles(data_path)
        sample = sample_population(all_profiles, n=population, seed=seed)
        log.info("Sampled %d profiles from %d", len(sample), len(all_profiles))

        # Save brief
        (session_dir / "brief.txt").write_text(brief)

        # Save compact profiles for quick inspection
        compact = []
        for p in sample:
            compact.append({
                "uid": p.get("uid", "unknown"),
                "dominant": p.get("dominant", p.get("role", "unknown")),
                "role": p.get("role", p.get("dominant", "")),
                "directive": p.get("directive", ""),
                "num_reviews": len(p.get("reviews", [])),
                "num_voice": len(p.get("voice", [])),
            })
        (session_dir / "profiles.json").write_text(json.dumps(compact, indent=2))

        # Save full profiles for reproduction
        with open(session_dir / "profiles_full.jsonl", "w") as f:
            for p in sample:
                f.write(json.dumps(p) + "\n")

        config = {
            "session_id": session_id,
            "data_path": str(data_path),
            "population": population,
            "seed": seed,
            "model": model,
            "timeout": timeout,
            "activation": activation,
            "current_round": 0,
            "status": "created",
            "tags": [],
            "created_at": datetime.now().isoformat(),
        }

        session = Session(session_id, config, session_dir)
        session._save_config()
        log.info("Session %s created at %s", session_id, session_dir)
        return session

    @staticmethod
    def create_from_profiles(
        profiles: list[dict],
        brief: str,
        model: str = "haiku",
        timeout: float = 60.0,
        activation: float = 0.2,
        analyze: bool = True,
    ) -> "Session":
        """Create a session from pre-assembled profiles (no sampling)."""
        session_id = f"sim_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        session_dir = SESSIONS_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        (session_dir / "brief.txt").write_text(brief)

        compact = []
        for p in profiles:
            uid = p.get("uid") or p.get("persona_id", "unknown")
            role = p.get("role") or p.get("tone") or p.get("dominant", "")
            compact.append({
                "uid": uid,
                "dominant": p.get("dominant", role),
                "role": role,
                "directive": p.get("directive", ""),
                "context": p.get("context", "full"),
                "num_reviews": len(p.get("reviews", [])),
                "num_voice": len(p.get("voice", [])),
            })
        (session_dir / "profiles.json").write_text(json.dumps(compact, indent=2))

        with open(session_dir / "profiles_full.jsonl", "w") as f:
            for p in profiles:
                f.write(json.dumps(p) + "\n")

        config = {
            "session_id": session_id,
            "data_path": "api",
            "population": len(profiles),
            "seed": 0,
            "model": model,
            "timeout": timeout,
            "activation": activation,
            "analyze": analyze,
            "current_round": 0,
            "status": "created",
            "tags": [],
            "created_at": datetime.now().isoformat(),
        }

        session = Session(session_id, config, session_dir)
        session._save_config()
        log.info("Session %s created from %d profiles at %s", session_id, len(profiles), session_dir)
        return session

    @staticmethod
    def load(session_id: str) -> "Session":
        """Load an existing session from disk."""
        session_dir = SESSIONS_DIR / session_id
        config_path = session_dir / "session.json"
        if not config_path.exists():
            raise FileNotFoundError(f"No session at {config_path}")
        config = json.loads(config_path.read_text())
        return Session(session_id, config, session_dir)

    @staticmethod
    def list_sessions() -> list[dict]:
        """List all sessions with basic info."""
        if not SESSIONS_DIR.exists():
            return []
        sessions = []
        for d in sorted(SESSIONS_DIR.iterdir()):
            config_path = d / "session.json"
            if config_path.exists():
                config = json.loads(config_path.read_text())
                sessions.append({
                    "session_id": config["session_id"],
                    "status": config["status"],
                    "population": config["population"],
                    "current_round": config["current_round"],
                    "tags": config.get("tags", []),
                })
        return sessions

    @staticmethod
    def fork(source_session_id: str, tag_name: str, name: str | None = None) -> "Session":
        """Fork a new session from a tagged snapshot.

        Creates an independent session with a copy of the tagged DB,
        same profiles and brief. Can run in parallel with the source.
        """
        source = Session.load(source_session_id)
        source_tag_db = source.session_dir / "sim" / source.session_id / "tags" / f"{tag_name}.db"
        if not source_tag_db.exists():
            raise FileNotFoundError(f"No tag '{tag_name}' in session {source_session_id}")

        # Determine round count from the tagged DB
        import sqlite3
        conn = sqlite3.connect(str(source_tag_db))
        row = conn.execute("SELECT MAX(round) FROM agent_rounds").fetchone()
        conn.close()
        forked_round = row[0] if row and row[0] else 0

        # Create new session dir
        if name:
            session_id = f"{source_session_id}_{name}"
        else:
            session_id = f"sim_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        session_dir = SESSIONS_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Copy brief and profiles from source
        shutil.copy2(source.session_dir / "brief.txt", session_dir / "brief.txt")
        shutil.copy2(source.session_dir / "profiles.json", session_dir / "profiles.json")
        shutil.copy2(source.session_dir / "profiles_full.jsonl", session_dir / "profiles_full.jsonl")

        # Copy tagged DB as the new session's live DB
        sim_dir = session_dir / "sim" / session_id
        sim_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_tag_db, sim_dir / "state.db")

        config = {
            "session_id": session_id,
            "data_path": source.config["data_path"],
            "population": source.config["population"],
            "seed": source.config["seed"],
            "model": source.config["model"],
            "timeout": source.config.get("timeout", 60.0),
            "current_round": forked_round,
            "status": "ready",
            "tags": [],
            "created_at": datetime.now().isoformat(),
            "forked_from": {"session": source_session_id, "tag": tag_name},
        }

        session = Session(session_id, config, session_dir)
        session._save_config()
        log.info("Forked %s:%s → %s (round %d)", source_session_id, tag_name, session_id, forked_round)
        return session

    # ── Lifecycle ─────────────────────────────────────────────

    async def _ensure_state(self) -> SimState:
        """Ensure in-memory SimState exists — create or resume as needed."""
        if self._state is not None:
            return self._state

        profiles = self._load_profiles()
        brief = self._load_brief()
        if self._model_backend:
            model = self._model_backend
        else:
            model = ClaudeCliModel(
                model=self.config["model"],
                timeout=self.config.get("timeout", 60.0),
            )
        sim_config = SimConfig(
            sim_base=self.sim_base,
            rounds=5,
            activation=self.config.get("activation", 0.2),
            model=model,
            analyze=self.config.get("analyze", True),
        )

        live = Path(self.live_db)
        if live.exists() and self.config["current_round"] > 0:
            self._state = await resume_simulation(
                self.session_id, brief, profiles, sim_config,
                current_round=self.config["current_round"],
            )
        else:
            self._state = await create_simulation(
                self.session_id, brief, profiles, sim_config,
            )

        return self._state

    async def run(self, rounds: int | None = None, until_converge: bool = False, live: bool = False, on_round: callable = None) -> int:
        """Run simulation rounds."""
        state = await self._ensure_state()

        if state.status == "created" or state.current_round == 0:
            # First run — inject brief
            brief = self._load_brief()
            await inject_post(state, brief)

        # Live HTML callback for sequential mode
        on_step = None
        if live:
            live_dir = self.session_dir / "reports" / "live"
            def on_step(s):
                generate_live_html(self.live_db, live_dir)

        executed, converged = await advance(state, rounds=rounds, until_converge=until_converge, on_step=on_step, on_round=on_round)

        self.config["current_round"] = state.current_round
        self.config["status"] = "converged" if converged else "ready"
        self._save_config()

        if converged:
            await self.tag("baseline")

        return executed

    async def inject(self, content: str) -> None:
        """Inject a product change into the simulation."""
        state = await self._ensure_state()
        await inject_post(state, content)
        log.info("Injected change into session %s", self.session_id)

    async def tag(self, name: str) -> None:
        """Tag the current simulation state."""
        state = await self._ensure_state()
        await tag_state(state, name)
        if name not in self.config["tags"]:
            self.config["tags"].append(name)
        self._save_config()

    async def revert(self, tag_name: str) -> None:
        """Revert to a tagged state. Clears in-memory state (rebuilt on next run)."""
        state = await self._ensure_state()
        await revert_state(state, tag_name)

        # Figure out round from DB
        import sqlite3
        conn = sqlite3.connect(self.live_db)
        row = conn.execute("SELECT MAX(round) FROM agent_rounds").fetchone()
        conn.close()
        restored_round = row[0] if row and row[0] else 0

        self.config["current_round"] = restored_round
        self.config["status"] = "ready"
        self._save_config()

        # Clear in-memory state — will be rebuilt on next run/inject
        await self.close()

    async def report(self, tag_name: str | None = None) -> Path:
        """Generate synthesis + HTML report for the current or tagged state."""
        profiles = self._load_profiles()

        report_tag = tag_name or (self.config["tags"][-1] if self.config["tags"] else "current")
        report_dir = self.session_dir / "reports" / report_tag
        report_dir.mkdir(parents=True, exist_ok=True)

        verdict = synthesize(
            self.live_db,
            agent_profiles=profiles,
            total_rounds=self.config["current_round"],
            model=self.config["model"],
        )
        save_synthesis(verdict, report_dir)

        # Markdown report
        md = render_report(verdict, self.live_db)
        (report_dir / "report.md").write_text(md)

        # HTML report
        html_path = generate_html_report(verdict, self.live_db, report_dir)

        log.info("Report written to %s (adoption=%.2f)", html_path, verdict.adoption_score)
        return html_path

    def status(self) -> dict:
        """Return session status."""
        return {
            "session_id": self.session_id,
            "status": self.config["status"],
            "population": self.config["population"],
            "current_round": self.config["current_round"],
            "model": self.config["model"],
            "tags": self.config.get("tags", []),
            "available_tags": list_tags(self.sim_base, self.session_id),
            "created_at": self.config["created_at"],
        }

    async def close(self) -> None:
        """Shut down the simulation environment."""
        if self._state:
            await close(self._state)
            self._state = None
