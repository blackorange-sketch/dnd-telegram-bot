"""Multiplayer room storage, backed by the same SQLite database as the
solo-play GameState (see game_state.py), in two tables of its own.

A room holds everything shared by the whole party (world, language,
summary, recent turns, full log, pending options) plus a "seat" per player
(their own character — hp/money/inventory/attributes, same shape as solo
play's character dict) and a turn order.

This module is purely additive: nothing in the solo-play path (main.py,
core.py, webapp.py) touches it yet. It is the data foundation the actual
multiplayer game loop will be built on top of in a follow-up step — the
turn-based mode: one player acts, then play passes to the next.
"""

import json
import random
import sqlite3
import string
import time
from dataclasses import dataclass, field

from game_state import DB_PATH, MAX_LOG_ENTRIES, MAX_RECENT_TURNS

ROOM_CODE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + string.digits.replace("0", "").replace("1", "")
ROOM_CODE_LENGTH = 6  # short enough to read aloud/type, no ambiguous O/0/I/1


def _generate_room_code() -> str:
    return "".join(random.choices(ROOM_CODE_ALPHABET, k=ROOM_CODE_LENGTH))


# In-memory-only heartbeat of when each seated player was last seen polling
# /api/room/state or acting (webapp.py calls touch_seen() on every such
# call). Deliberately NOT persisted to SQLite — it's just a heuristic for
# whether a "your turn" push notification is worth sending (see webapp.py's
# _notify_turn), and losing it on a restart only costs one possibly-redundant
# notification, not a correctness problem.
_LAST_SEEN: dict[tuple[str, int], float] = {}


def touch_seen(room_id: str, user_id: int) -> None:
    _LAST_SEEN[(room_id, user_id)] = time.monotonic()


def seconds_since_seen(room_id: str, user_id: int) -> float:
    ts = _LAST_SEEN.get((room_id, user_id))
    if ts is None:
        return float("inf")
    return time.monotonic() - ts


@dataclass
class Seat:
    user_id: int
    display_name: str
    character: dict = field(default_factory=dict)


@dataclass
class RoomState:
    room_id: str
    host_user_id: int
    language: str = "uk"
    category: str = ""
    world_description: str | None = None
    summary: str = ""
    recent_turns: list[str] = field(default_factory=list)
    full_log: list[str] = field(default_factory=list)
    turn_count: int = 0
    pending_options: list[dict] = field(default_factory=list)
    seats: dict[int, Seat] = field(default_factory=dict)  # key: user_id
    turn_order: list[int] = field(default_factory=list)  # user_ids, play order
    current_turn_index: int = 0
    started: bool = False  # False = still in the lobby, waiting for the host to start
    # Journal: shared world-state facts for the whole party (see
    # gemini_client.py's update_journal function) — unlike character state,
    # these belong to the room, not any one seat.
    location: dict | None = None  # {"name": ..., "description": ...} or None
    visited_locations: list[str] = field(default_factory=list)
    npcs: dict = field(default_factory=dict)  # name -> {"description": ..., "relationship": ...}
    quests: dict = field(default_factory=dict)  # title -> {"status": ..., "description": ...}

    def current_turn_user_id(self) -> int | None:
        if not self.turn_order:
            return None
        return self.turn_order[self.current_turn_index % len(self.turn_order)]

    def is_turn(self, user_id: int) -> bool:
        return self.current_turn_user_id() == user_id

    def advance_turn(self) -> None:
        if self.turn_order:
            self.current_turn_index = (self.current_turn_index + 1) % len(self.turn_order)

    def add_turn(self, text: str) -> None:
        """Mirrors GameState.add_turn: a short rolling buffer sent to Gemini,
        plus a much longer log kept only for the players to scroll back."""
        self.recent_turns.append(text)
        if len(self.recent_turns) > MAX_RECENT_TURNS:
            self.recent_turns.pop(0)
        self.full_log.append(text)
        if len(self.full_log) > MAX_LOG_ENTRIES:
            self.full_log.pop(0)

    def to_json(self) -> str:
        return json.dumps({
            "host_user_id": self.host_user_id,
            "language": self.language,
            "category": self.category,
            "world_description": self.world_description,
            "summary": self.summary,
            "recent_turns": self.recent_turns,
            "full_log": self.full_log,
            "turn_count": self.turn_count,
            "pending_options": self.pending_options,
            "seats": {
                str(uid): {"display_name": seat.display_name, "character": seat.character}
                for uid, seat in self.seats.items()
            },
            "turn_order": self.turn_order,
            "current_turn_index": self.current_turn_index,
            "started": self.started,
            "location": self.location,
            "visited_locations": self.visited_locations,
            "npcs": self.npcs,
            "quests": self.quests,
        })

    @classmethod
    def from_json(cls, room_id: str, raw: str) -> "RoomState":
        data = json.loads(raw)
        seats = {
            int(uid): Seat(user_id=int(uid), display_name=s.get("display_name", ""), character=s.get("character", {}))
            for uid, s in data.get("seats", {}).items()
        }
        return cls(
            room_id=room_id,
            host_user_id=data["host_user_id"],
            language=data.get("language", "uk"),
            category=data.get("category", ""),
            world_description=data.get("world_description"),
            summary=data.get("summary", ""),
            recent_turns=data.get("recent_turns", []),
            full_log=data.get("full_log", []),
            turn_count=data.get("turn_count", 0),
            pending_options=data.get("pending_options", []),
            seats=seats,
            turn_order=data.get("turn_order", []),
            current_turn_index=data.get("current_turn_index", 0),
            started=data.get("started", False),
            location=data.get("location"),
            visited_locations=data.get("visited_locations", []),
            npcs=data.get("npcs", {}),
            quests=data.get("quests", {}),
        )


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            room_id TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS room_members (
            user_id INTEGER PRIMARY KEY,
            room_id TEXT NOT NULL
        )
        """
    )
    return conn


def create_room(host_user_id: int, host_display_name: str, language: str, category: str, world_description: str | None) -> RoomState:
    """Create a new room with the host as its first (and so far only) seat.
    The room code is guaranteed unique among currently-stored rooms."""
    conn = _connect()
    try:
        while True:
            room_id = _generate_room_code()
            exists = conn.execute("SELECT 1 FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
            if not exists:
                break
    finally:
        conn.close()

    room = RoomState(
        room_id=room_id,
        host_user_id=host_user_id,
        language=language,
        category=category,
        world_description=world_description,
        seats={host_user_id: Seat(user_id=host_user_id, display_name=host_display_name)},
        turn_order=[host_user_id],
    )
    save_room(room)
    _set_member(host_user_id, room_id)
    return room


def load_room(room_id: str) -> RoomState | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT data FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
        if row is None:
            return None
        return RoomState.from_json(room_id, row[0])
    finally:
        conn.close()


def save_room(room: RoomState) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO rooms (room_id, data) VALUES (?, ?)
            ON CONFLICT(room_id) DO UPDATE SET data = excluded.data
            """,
            (room.room_id, room.to_json()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_room(room_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM room_members WHERE room_id = ?", (room_id,))
        conn.commit()
    finally:
        conn.close()


def _set_member(user_id: int, room_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO room_members (user_id, room_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET room_id = excluded.room_id
            """,
            (user_id, room_id),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_member(user_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM room_members WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def room_id_for_user(user_id: int) -> str | None:
    """Which room (if any) this user currently belongs to — a user can only
    be seated in one room at a time."""
    conn = _connect()
    try:
        row = conn.execute("SELECT room_id FROM room_members WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def load_room_for_user(user_id: int) -> RoomState | None:
    room_id = room_id_for_user(user_id)
    return load_room(room_id) if room_id else None


def join_room(room_id: str, user_id: int, display_name: str) -> RoomState | None:
    """Add a user to an existing room's turn order, giving them a fresh seat
    only if they don't already have one — a player rejoining after
    leave_room() keeps their existing character (hp/money/inventory) instead
    of starting over. Returns the updated room, or None if it doesn't
    exist."""
    room = load_room(room_id)
    if room is None:
        return None
    if user_id not in room.seats:
        room.seats[user_id] = Seat(user_id=user_id, display_name=display_name)
    else:
        room.seats[user_id].display_name = display_name
    if user_id not in room.turn_order:
        room.turn_order.append(user_id)
    save_room(room)
    _set_member(user_id, room_id)
    return room


def leave_room(room_id: str, user_id: int) -> RoomState | None:
    """Take a player out of the active turn order so play isn't blocked
    waiting on them, but keep their seat (character) intact — rejoining
    later with the same room code via join_room() restores it rather than
    generating a new character. If the host leaves, the next player in turn
    order (if any) becomes the new host. The room itself is never deleted
    here, so the code stays valid for whoever wants to come back. Returns
    the updated room, or None if it didn't exist."""
    room = load_room(room_id)
    if room is None:
        return None

    if user_id in room.turn_order:
        idx = room.turn_order.index(user_id)
        room.turn_order.remove(user_id)
        if room.turn_order:
            if idx <= room.current_turn_index:
                room.current_turn_index = room.current_turn_index % len(room.turn_order)
        else:
            room.current_turn_index = 0

    _clear_member(user_id)

    if room.host_user_id == user_id and room.turn_order:
        room.host_user_id = room.turn_order[0]

    save_room(room)
    return room
