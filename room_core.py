"""Multiplayer (turn-based) game logic, mirroring core.py's solo-play
functions but operating on a RoomState with multiple seated players
instead of a single GameState.

Design decision: each turn's [STATE] tag still describes exactly ONE
character (the acting player's), not the whole party — this reuses the
existing solo-play STATE parsing logic unchanged rather than inventing a
fragile multi-character state format. Other seats' characters only change
on their own turn.
"""

import logging

import core
import gemini_client
import languages as lang
import world_categories as wc
from game_state import SUMMARY_EVERY_N_TURNS
from rooms import RoomState, Seat, save_room

logger = logging.getLogger(__name__)


class NotYourTurnError(Exception):
    """Raised when a player tries to act out of turn."""


def _party_roster_note(room: RoomState, acting_user_id: int) -> str | None:
    """A short 'Name: description (acting now)' roster string so Gemini can
    acknowledge other party members narratively, or None for a solo room."""
    if len(room.seats) < 2:
        return None
    parts = []
    for uid in room.turn_order:
        seat = room.seats.get(uid)
        if not seat:
            continue
        who = seat.character.get("description") or "an adventurer"
        marker = " (acting now)" if uid == acting_user_id else ""
        parts.append(f"{seat.display_name}: {who}{marker}")
    return "; ".join(parts) if parts else None


def party_status(room: RoomState) -> list[dict]:
    """Compact per-seat summary: user_id, display_name, hp, max_hp, is_turn."""
    active = room.current_turn_user_id()
    return [
        {
            "user_id": uid,
            "display_name": room.seats[uid].display_name,
            "hp": room.seats[uid].character.get("hp"),
            "max_hp": room.seats[uid].character.get("max_hp"),
            "is_turn": uid == active,
        }
        for uid in room.turn_order
        if uid in room.seats
    ]


async def maybe_summarize_room(room: RoomState) -> None:
    """Mirrors core.maybe_summarize for a room's shared history."""
    if room.turn_count == 0 or room.turn_count % SUMMARY_EVERY_N_TURNS != 0:
        return
    try:
        room.summary = await gemini_client.generate_summary(
            existing_summary=room.summary,
            recent_turns=room.recent_turns,
            language_name=lang.prompt_name_for(room.language),
        )
        room.recent_turns = room.recent_turns[-2:]
    except Exception:
        logger.exception("Room summary generation failed, skipping this round")


async def perform_room_turn(room: RoomState, acting_user_id: int, player_input: str) -> dict:
    """Advance a multiplayer room's story by one turn, from the acting
    player's seat. Raises NotYourTurnError if it isn't their turn. Raises
    whatever Gemini raises on failure (caller decides how to surface it) —
    the room is NOT mutated/saved in that case."""
    if not room.is_turn(acting_user_id):
        raise NotYourTurnError(f"It is not user {acting_user_id}'s turn")

    seat = room.seats[acting_user_id]
    lang_key = room.language
    category_hint = wc.short_hint_for(room.category) if room.category else None
    party_note = _party_roster_note(room, acting_user_id)

    room.add_turn(f"[{seat.display_name}]: {player_input}")

    prev_hp = seat.character.get("hp")
    prev_money = seat.character.get("money")
    prev_inventory = list(seat.character.get("inventory") or [])

    try:
        story_text = await gemini_client.generate_story_turn(
            summary=room.summary,
            recent_turns=room.recent_turns,
            character=seat.character,
            player_input=player_input,
            language_name=lang.prompt_name_for(lang_key),
            category_hint=category_hint,
            party_note=party_note,
        )
        used_last_resort = False
    except gemini_client.ContentBlockedError:
        logger.warning("Room turn blocked by safety filter, retrying once with a softened prompt")
        try:
            story_text = await gemini_client.generate_story_turn(
                summary=room.summary,
                recent_turns=room.recent_turns,
                character=seat.character,
                player_input=player_input,
                language_name=lang.prompt_name_for(lang_key),
                category_hint=category_hint,
                party_note=party_note,
                soften=True,
            )
            used_last_resort = False
        except gemini_client.ContentBlockedError:
            logger.warning("Still blocked after softening; retrying once more without raw recent-turn history")
            story_text = await gemini_client.generate_story_turn(
                summary=room.summary,
                recent_turns=[],
                character=seat.character,
                player_input=player_input,
                language_name=lang.prompt_name_for(lang_key),
                category_hint=category_hint,
                party_note=party_note,
                soften=True,
            )
            used_last_resort = True

    clean_text, parsed_state = gemini_client.parse_state_tag(story_text)
    if "hp" in parsed_state:
        if not seat.character.get("max_hp"):
            seat.character["max_hp"] = parsed_state.get("max_hp")
        max_hp = seat.character.get("max_hp") or parsed_state["hp"]
        seat.character["hp"] = max(0, min(parsed_state["hp"], max_hp))
    if "money" in parsed_state:
        seat.character["money"] = parsed_state["money"]
    if "inventory" in parsed_state:
        seat.character["inventory"] = parsed_state["inventory"]

    changes = core.compute_changes(prev_hp, prev_money, prev_inventory, seat.character)
    display_text, options = core.format_options(clean_text, lang_key)

    if used_last_resort:
        room.recent_turns = [f"[{seat.display_name}]: {player_input}"]

    # Store the cleaned narration (options stripped), not the raw clean_text,
    # so option lines aren't duplicated into the shared turn history/log.
    room.add_turn(f"[DM]: {display_text}")
    room.pending_options = options
    room.turn_count += 1
    await maybe_summarize_room(room)
    room.advance_turn()
    save_room(room)

    hp = seat.character.get("hp")
    return {
        "text": display_text,
        "options": options,
        "character": seat.character,
        "defeated": hp is not None and hp <= 0,
        "log": room.full_log,
        "changes": changes,
        "changes_line": core.format_changes_line(changes),
        "active_user_id": room.current_turn_user_id(),
        "party": party_status(room),
    }


async def create_room_adventure(
    room: RoomState,
    character_description: str | None,
    gender: str | None,
    age: str | None,
) -> dict:
    """Generate the opening scene for a brand-new room, using the host's
    character. Mirrors core.create_adventure closely."""
    language_name = lang.prompt_name_for(room.language)
    character_brief, character_record = core.build_character_brief(character_description, gender, age)

    try:
        opening = await gemini_client.generate_new_adventure_opening(
            category_label=wc.label_for(room.category, room.language),
            category_hint=wc.hint_for(room.category),
            world_description=room.world_description,
            character_brief=character_brief,
            language_name=language_name,
        )
    except gemini_client.ContentBlockedError:
        logger.warning("Room opening blocked by safety filter, retrying once with a softened prompt")
        opening = await gemini_client.generate_new_adventure_opening(
            category_label=wc.label_for(room.category, room.language),
            category_hint=wc.hint_for(room.category),
            world_description=room.world_description,
            character_brief=character_brief,
            language_name=language_name,
            soften=True,
        )

    text_after_state, parsed_state = gemini_client.parse_state_tag(opening)
    clean_text, attrs = gemini_client.parse_attrs_tag(text_after_state)
    hp = parsed_state.get("hp", gemini_client.DEFAULT_HP)
    max_hp = parsed_state.get("max_hp", gemini_client.DEFAULT_HP)

    character_record["category"] = room.category
    character_record["world_description"] = room.world_description
    character_record["hp"] = hp
    character_record["max_hp"] = max_hp
    if "money" in parsed_state:
        character_record["money"] = parsed_state["money"]
    if "inventory" in parsed_state:
        character_record["inventory"] = parsed_state["inventory"]
    if attrs:
        character_record["attributes"] = attrs

    display_text, options = core.format_options(clean_text, room.language)

    host_seat = room.seats[room.host_user_id]
    host_seat.character = character_record
    room.add_turn(f"[DM]: {display_text}")
    room.pending_options = options
    save_room(room)

    return {
        "text": display_text,
        "options": options,
        "character": host_seat.character,
        "defeated": False,
        "log": room.full_log,
        "active_user_id": room.current_turn_user_id(),
        "party": party_status(room),
    }


async def create_joining_character(
    room: RoomState,
    seat: Seat,
    character_description: str | None,
    gender: str | None,
    age: str | None,
) -> dict:
    """Generate a character for a player who just joined an in-progress
    room, fitting the world already established by the host."""
    language_name = lang.prompt_name_for(room.language)
    character_brief, character_record = core.build_character_brief(character_description, gender, age)
    character_brief += (
        " This character is JOINING an adventure already in progress, not starting a new one — "
        "just invent the character themselves, fitting the established world."
    )

    if character_description:
        preview = character_description
    else:
        preview = await gemini_client.generate_character_preview(
            gender=gender or "any",
            age=age or "any",
            world_description=room.world_description,
            category_hint=wc.hint_for(room.category),
            language_name=language_name,
        )

    character_record["category"] = room.category
    character_record["world_description"] = room.world_description
    character_record["hp"] = gemini_client.DEFAULT_HP
    character_record["max_hp"] = gemini_client.DEFAULT_HP
    character_record["description"] = character_record.get("description") or preview

    seat.character = character_record
    save_room(room)

    return {"character": seat.character, "party": party_status(room)}
