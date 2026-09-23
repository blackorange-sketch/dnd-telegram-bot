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

DB_PATH = Path(__file__).parent / "game_data.sqlite3"

MAX_RECENT_TURNS = 6  # how many raw turns to keep verbatim before summarizing


@dataclass
class GameState:
    user_id: int
    character: dict = field(default_factory=dict)
    summary: str = ""
    recent_turns: list[str] = field(default_factory=list)
    language: str = "Ukrainian"

    def to_json(self) -> str:
        return json.dumps({
            "character": self.character,
            "summary": self.summary,
            "recent_turns": self.recent_turns,
            "language": self.language,
        })

    @classmethod
    def from_json(cls, user_id: int, raw: str) -> "GameState":
        data = json.loads(raw)
        return cls(
            user_id=user_id,
            character=data.get("character", {}),
            summary=data.get("summary", ""),
            recent_turns=data.get("recent_turns", []),
            language=data.get("language", "Ukrainian"),
        )

    def add_turn(self, text: str) -> None:
        self.recent_turns.append(text)
        if len(self.recent_turns) > MAX_RECENT_TURNS:
            # Drop the oldest turn. For a real app you'd summarize it into
            # `self.summary` via an extra Gemini call instead of discarding it.
            self.recent_turns.pop(0)


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
