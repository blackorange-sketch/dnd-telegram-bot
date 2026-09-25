"""Per-user game state storage backed by SQLite.

Each Telegram user gets their own row with a JSON blob holding:
- a running story summary (kept short so the prompt doesn't grow forever)
- the last few raw exchanges (for near-term continuity)
- basic character info (name, class, hp, etc.)
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

"""Per-user game state storage backed by SQLite.

Each Telegram user gets their own row with a JSON blob holding:
- a running story summary (kept short so the prompt doesn't grow forever)
- the last few raw exchanges (for near-term continuity, sent to Gemini)
- a longer full_log kept purely for the player to scroll back through
- basic character info (name, class, hp, etc.)

The SQLite file lives under DATA_DIR (default: next to this file). On
Railway, the container filesystem is wiped on every redeploy — set the
DATA_DIR environment variable to a mounted Volume's path (e.g. "/data") to
survive redeploys and restarts.
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "game_data.sqlite3"

MAX_RECENT_TURNS = 6  # how many raw turns to keep verbatim before summarizing
MAX_LOG_ENTRIES = 60  # how many turns to keep for the player-facing history view

SUMMARY_EVERY_N_TURNS = 12  # how often to compress recent_turns into `summary`


@dataclass
class GameState:
    user_id: int
    character: dict = field(default_factory=dict)
    summary: str = ""
    recent_turns: list[str] = field(default_factory=list)
    full_log: list[str] = field(default_factory=list)
    language: str = "uk"
    turn_count: int = 0
    pending_options: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "character": self.character,
            "summary": self.summary,
            "recent_turns": self.recent_turns,
            "full_log": self.full_log,
            "language": self.language,
            "turn_count": self.turn_count,
            "pending_options": self.pending_options,
        })

    @classmethod
    def from_json(cls, user_id: int, raw: str) -> "GameState":
        data = json.loads(raw)
        return cls(
            user_id=user_id,
            character=data.get("character", {}),
            summary=data.get("summary", ""),
            recent_turns=data.get("recent_turns", []),
            full_log=data.get("full_log", []),
            language=data.get("language", "uk"),
            turn_count=data.get("turn_count", 0),
            pending_options=data.get("pending_options", []),
        )

    def add_turn(self, text: str) -> None:
        self.recent_turns.append(text)
        if len(self.recent_turns) > MAX_RECENT_TURNS:
            # Drop the oldest turn from the short-term buffer sent to Gemini.
            # For a real app you'd summarize it into `self.summary` via an
            # extra Gemini call instead of discarding it (see core.py).
            self.recent_turns.pop(0)

        # Kept separately, with a much longer cap, purely so the player can
        # scroll back through what happened — never used in Gemini prompts.
        self.full_log.append(text)
        if len(self.full_log) > MAX_LOG_ENTRIES:
            self.full_log.pop(0)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS game_state (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL
        )
        """
    )
    return conn


def load_state(user_id: int) -> GameState | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT data FROM game_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return GameState.from_json(user_id, row[0])
    finally:
        conn.close()


def save_state(state: GameState) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO game_state (user_id, data) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET data = excluded.data
            """,
            (state.user_id, state.to_json()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_state(user_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM game_state WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
