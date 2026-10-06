"""HTTP side of the bot: serves the Mini App's static page and a small
JSON API it calls (/api/state, /api/action). Runs in the same process as
the Telegram polling bot — see main.py's `main()`.
"""

import logging
import os
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiohttp import web

import core
import gemini_client
import languages as lang
import room_core
import rooms
import ui_strings as ui
import world_categories as wc
from game_state import load_state
from telegram_auth import verify_init_data

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
STATIC_DIR = Path(__file__).parent / "static"

routes = web.RouteTableDef()

# Set once at startup from main.py (set_bot()) so API handlers here can push
# a Telegram message on their own — e.g. "it's your turn" — without webapp.py
# needing to own the bot/polling lifecycle itself.
_bot = None
BOT_USERNAME: str | None = None


def set_bot(bot_instance, username: str | None) -> None:
    global _bot, BOT_USERNAME
    _bot = bot_instance
    BOT_USERNAME = username


async def _notify_turn(room: "rooms.RoomState", user_id: int) -> None:
    """Push a 'your turn' Telegram message to a seated player, but only if
    they don't already seem to be in the app — if they were polling the room
    just a few seconds ago, normal in-app polling will show them the new
    turn anyway, and a push on top of that would just be noise."""
    if _bot is None or not PUBLIC_URL:
        return
    if rooms.seconds_since_seen(room.room_id, user_id) < 8:
        return
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=ui.t(room.language, "open_miniapp_button"),
                web_app=WebAppInfo(url=f"{PUBLIC_URL.rstrip('/')}/miniapp"),
            )
        ]])
        await _bot.send_message(
            user_id,
            ui.t(room.language, "your_turn_push", room=room.room_id),
            reply_markup=kb,
        )
    except Exception:
        logger.exception("Failed to send turn-notification push to %s", user_id)


def _authed_user(body: dict) -> dict | None:
    init_data = body.get("initData", "")
    return verify_init_data(init_data, BOT_TOKEN)


def _authed_user_id(body: dict) -> int | None:
    user = _authed_user(body)
    if not user:
        return None
    return user.get("id")


def _display_name(user: dict) -> str:
    name = user.get("first_name") or user.get("username")
    if not name:
        return f"Player {user.get('id')}"
    return name


def _last_dm_text(game) -> str:
    for turn in reversed(game.recent_turns):
        if turn.startswith("[DM]:"):
            return turn[len("[DM]:"):].strip()
    return ""


@routes.get("/")
@routes.get("/miniapp")
async def serve_miniapp(request: web.Request) -> web.Response:
    # Telegram's in-app WebView caches this page aggressively across
    # deploys — force a revalidation every time so a redeploy is actually
    # visible without the player having to clear app data.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@routes.post("/api/state")
async def api_state(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user_id = _authed_user_id(body)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    game = load_state(user_id)
    if game is None:
        return web.json_response({"error": "no_active_game"}, status=404)

    return web.json_response({
        "text": _last_dm_text(game),
        "options": game.pending_options,
        "character": game.character,
        "language": game.language,
        "log": game.full_log,
        "journal": core.journal_payload(game),
    })


# ---------------------------------------------------------------------------
# Static reference data for the onboarding wizard (no auth needed — nothing
# user-specific here, just the same labels/strings the bot itself uses).
# ---------------------------------------------------------------------------

@routes.get("/api/languages")
async def api_languages(request: web.Request) -> web.Response:
    return web.json_response([
        {"key": key, "label": lang.label_for(key)} for key in lang.all_keys()
    ])


@routes.get("/api/config")
async def api_config(request: web.Request) -> web.Response:
    """Small bits of server config the frontend needs but isn't worth a
    dedicated endpoint for — currently just the bot's @username, so the
    room-invite share button can build a t.me deep link."""
    return web.json_response({"bot_username": BOT_USERNAME})


@routes.get("/api/categories")
async def api_categories(request: web.Request) -> web.Response:
    lang_key = request.query.get("lang", lang.DEFAULT_LANGUAGE)
    categories = [
        {"key": key, "label": wc.label_for(key, lang_key)} for key in wc.all_keys()
    ]
    categories.append({"key": wc.RANDOM_KEY, "label": wc.random_label(lang_key)})
    return web.json_response(categories)


@routes.get("/api/ui")
async def api_ui(request: web.Request) -> web.Response:
    lang_key = request.query.get("lang", lang.DEFAULT_LANGUAGE)
    return web.json_response(ui.STRINGS.get(lang_key, ui.STRINGS[ui.DEFAULT_UI_LANG]))


# ---------------------------------------------------------------------------
# Onboarding: preview + regenerate world/character concepts, then create
# ---------------------------------------------------------------------------

@routes.post("/api/preview/world")
async def api_preview_world(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)
    if _authed_user_id(body) is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    lang_key = body.get("language") or lang.DEFAULT_LANGUAGE
    category_key = body.get("category") or wc.all_keys()[0]
    try:
        preview = await gemini_client.generate_world_preview(
            category_label=wc.label_for(category_key, lang_key),
            category_hint=wc.hint_for(category_key),
            language_name=lang.prompt_name_for(lang_key),
        )
    except Exception as e:
        logger.exception("Gemini error generating world preview")
        return web.json_response({"error": "gemini_error", "detail": str(e)}, status=502)
    return web.json_response({"preview": preview})


@routes.post("/api/preview/character")
async def api_preview_character(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)
    if _authed_user_id(body) is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    lang_key = body.get("language") or lang.DEFAULT_LANGUAGE
    category_key = body.get("category") or wc.all_keys()[0]
    try:
        preview = await gemini_client.generate_character_preview(
            gender=body.get("gender", "any"),
            age=body.get("age", "any"),
            world_description=body.get("world_description"),
            category_hint=wc.hint_for(category_key),
            language_name=lang.prompt_name_for(lang_key),
        )
    except Exception as e:
        logger.exception("Gemini error generating character preview")
        return web.json_response({"error": "gemini_error", "detail": str(e)}, status=502)
    return web.json_response({"preview": preview})


@routes.post("/api/create")
async def api_create(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user_id = _authed_user_id(body)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    lang_key = body.get("language") or lang.DEFAULT_LANGUAGE
    category_key = body.get("category") or wc.all_keys()[0]

    try:
        result = await core.create_adventure(
            user_id=user_id,
            language_key=lang_key,
            category_key=category_key,
            world_description=body.get("world_description"),
            character_description=body.get("character_description"),
            gender=body.get("gender"),
            age=body.get("age"),
        )
    except Exception as e:
        logger.exception("Gemini error creating adventure")
        return web.json_response({"error": "gemini_error", "detail": str(e)}, status=502)

    return web.json_response(result)


@routes.post("/api/end")
async def api_end(request: web.Request) -> web.Response:
    """End the player's current solo adventure (if any) so they can start a
    fresh one from the beginning. Generates a short epilogue for wherever
    they left off before deleting the save. A multiplayer room is left via
    the separate /api/room/leave, not this endpoint."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    user_id = _authed_user_id(body)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    result = await core.end_adventure_now(user_id)
    return web.json_response({"ok": True, "epilogue": result.get("epilogue")})


@routes.post("/api/action")
async def api_action(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user_id = _authed_user_id(body)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    game = load_state(user_id)
    if game is None:
        return web.json_response({"error": "no_active_game"}, status=404)

    action_type = body.get("type")
    roll_info = None

    if action_type == "choice":
        try:
            idx = int(body.get("index", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid_index"}, status=400)

        option = None
        if game.pending_options and 1 <= idx <= len(game.pending_options):
            option = game.pending_options[idx - 1]

        if option is None:
            player_input = f"Choosing option {idx}"
        elif option.get("requires_roll"):
            roll_info = core.compute_roll(game, attribute=option.get("attribute"))
            player_input = f"{option['text']} — {core.roll_action_text(roll_info)}"
        else:
            player_input = option["text"]

    elif action_type == "roll":
        try:
            sides = int(body.get("sides", 20))
        except (TypeError, ValueError):
            sides = 20
        roll_info = core.compute_roll(game, sides)
        player_input = f"I roll a die — {core.roll_action_text(roll_info)}"

    elif action_type == "text":
        player_input = str(body.get("text", "")).strip()
        if not player_input:
            return web.json_response({"error": "empty_text"}, status=400)

    else:
        return web.json_response({"error": "invalid_type"}, status=400)

    try:
        result = await core.perform_turn(game, player_input)
    except Exception as e:
        logger.exception("Gemini error in Mini App action")
        error_key = "content_blocked" if isinstance(e, gemini_client.ContentBlockedError) else "gemini_error"
        # The failed turn was never saved, so game.pending_options/character
        # here are still the previous (valid) ones — send them back so the
        # frontend can restore the option buttons instead of leaving a blank
        # screen with no way to proceed except retyping.
        return web.json_response({
            "error": error_key,
            "detail": str(e),
            "options": game.pending_options,
            "character": game.character,
        }, status=502)

    if roll_info:
        result["roll"] = roll_info
    return web.json_response(result)


# ---------------------------------------------------------------------------
# Multiplayer (turn-based rooms)
# ---------------------------------------------------------------------------

def _room_render(room: rooms.RoomState, user_id: int) -> dict:
    """Common shape for /api/room/state and the create/join/leave replies.
    Works both before the adventure starts (lobby: text/options empty) and
    after (started: a normal story render, same shape /api/state uses)."""
    seat = room.seats.get(user_id)
    return {
        "room_id": room.room_id,
        "host_user_id": room.host_user_id,
        "language": room.language,
        "started": room.started,
        "text": _last_dm_text(room),
        "options": room.pending_options,
        "character": seat.character if seat else None,
        "log": room.full_log,
        "active_user_id": room.current_turn_user_id(),
        "is_turn": room.is_turn(user_id),
        "party": room_core.party_status(room),
        "journal": core.journal_payload(room),
    }


@routes.post("/api/room/create")
async def api_room_create(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user = _authed_user(body)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    user_id = user["id"]

    if rooms.room_id_for_user(user_id):
        return web.json_response({"error": "already_in_room"}, status=409)

    lang_key = body.get("language") or lang.DEFAULT_LANGUAGE
    category_key = body.get("category") or wc.all_keys()[0]

    room = rooms.create_room(
        host_user_id=user_id,
        host_display_name=_display_name(user),
        language=lang_key,
        category=category_key,
        world_description=body.get("world_description"),
    )

    # The host's character is generated right away, but the shared opening
    # scene waits for the host to press "start" from the lobby — that way
    # the opening can introduce every player who joined in time, together,
    # instead of just the host with everyone else bolted on afterward.
    seat = room.seats[user_id]
    try:
        await room_core.create_character_for_seat(
            room, seat,
            character_description=body.get("character_description"),
            gender=body.get("gender"),
            age=body.get("age"),
            in_progress=False,
        )
        rooms.save_room(room)
    except Exception as e:
        logger.exception("Gemini error creating host character")
        rooms.delete_room(room.room_id)
        return web.json_response({"error": "gemini_error", "detail": str(e)}, status=502)

    rooms.touch_seen(room.room_id, user_id)
    return web.json_response(_room_render(room, user_id))


@routes.post("/api/room/join")
async def api_room_join(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user = _authed_user(body)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    user_id = user["id"]

    existing_room_id = rooms.room_id_for_user(user_id)
    room_code = str(body.get("room_id", "")).strip().upper()
    if not room_code:
        return web.json_response({"error": "missing_room_id"}, status=400)

    if existing_room_id == room_code:
        # Rejoining a room they're already seated in (e.g. reopened the Mini
        # App) — just return the current state, don't regenerate a character.
        room = rooms.load_room(room_code)
        if room is None:
            return web.json_response({"error": "room_not_found"}, status=404)
        return web.json_response(_room_render(room, user_id))

    room = rooms.join_room(room_code, user_id, _display_name(user))
    if room is None:
        return web.json_response({"error": "room_not_found"}, status=404)

    seat = room.seats[user_id]
    if not seat.character:
        try:
            await room_core.create_character_for_seat(
                room, seat,
                character_description=body.get("character_description"),
                gender=body.get("gender"),
                age=body.get("age"),
                in_progress=room.started,
            )
            rooms.save_room(room)
        except Exception as e:
            logger.exception("Gemini error creating joining character")
            return web.json_response({"error": "gemini_error", "detail": str(e)}, status=502)

    rooms.touch_seen(room.room_id, user_id)
    return web.json_response(_room_render(room, user_id))


@routes.post("/api/room/start")
async def api_room_start(request: web.Request) -> web.Response:
    """Host-only: leave the lobby and generate the shared opening scene for
    everyone currently seated."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user_id = _authed_user_id(body)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    room = rooms.load_room_for_user(user_id)
    if room is None:
        return web.json_response({"error": "no_active_room"}, status=404)
    if room.host_user_id != user_id:
        return web.json_response({"error": "not_host"}, status=403)
    if room.started:
        return web.json_response({"error": "already_started"}, status=409)
    if not room.turn_order:
        return web.json_response({"error": "no_players"}, status=400)

    try:
        result = await room_core.start_room_adventure(room)
    except Exception as e:
        logger.exception("Gemini error starting room adventure")
        error_key = "content_blocked" if isinstance(e, gemini_client.ContentBlockedError) else "gemini_error"
        return web.json_response({"error": error_key, "detail": str(e)}, status=502)

    result["room_id"] = room.room_id
    result["host_user_id"] = room.host_user_id
    result["is_turn"] = room.is_turn(user_id)
    seat = room.seats.get(user_id)
    result["character"] = seat.character if seat else None

    rooms.touch_seen(room.room_id, user_id)
    next_user_id = result.get("active_user_id")
    if next_user_id is not None and next_user_id != user_id:
        await _notify_turn(room, next_user_id)

    return web.json_response(result)


@routes.post("/api/room/state")
async def api_room_state(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user_id = _authed_user_id(body)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    room = rooms.load_room_for_user(user_id)
    if room is None:
        return web.json_response({"error": "no_active_room"}, status=404)

    # Heartbeat: the frontend polls this endpoint every few seconds while the
    # app is open, so this is what _notify_turn checks to decide whether a
    # player already seems to be looking at the game (see its docstring).
    rooms.touch_seen(room.room_id, user_id)

    return web.json_response(_room_render(room, user_id))


@routes.post("/api/room/action")
async def api_room_action(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user_id = _authed_user_id(body)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    room = rooms.load_room_for_user(user_id)
    if room is None:
        return web.json_response({"error": "no_active_room"}, status=404)
    if not room.started:
        return web.json_response({"error": "not_started"}, status=409)

    if not room.is_turn(user_id):
        return web.json_response({
            "error": "not_your_turn",
            "active_user_id": room.current_turn_user_id(),
            "party": room_core.party_status(room),
        }, status=409)

    seat = room.seats[user_id]
    action_type = body.get("type")
    roll_info = None

    if action_type == "choice":
        try:
            idx = int(body.get("index", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid_index"}, status=400)

        option = None
        if room.pending_options and 1 <= idx <= len(room.pending_options):
            option = room.pending_options[idx - 1]

        if option is None:
            player_input = f"Choosing option {idx}"
        elif option.get("requires_roll"):
            roll_info = core.compute_roll(seat, attribute=option.get("attribute"))
            player_input = f"{option['text']} — {core.roll_action_text(roll_info)}"
        else:
            player_input = option["text"]

    elif action_type == "roll":
        try:
            sides = int(body.get("sides", 20))
        except (TypeError, ValueError):
            sides = 20
        roll_info = core.compute_roll(seat, sides)
        player_input = f"I roll a die — {core.roll_action_text(roll_info)}"

    elif action_type == "text":
        player_input = str(body.get("text", "")).strip()
        if not player_input:
            return web.json_response({"error": "empty_text"}, status=400)

    else:
        return web.json_response({"error": "invalid_type"}, status=400)

    try:
        result = await room_core.perform_room_turn(room, user_id, player_input)
    except room_core.NotYourTurnError:
        return web.json_response({
            "error": "not_your_turn",
            "active_user_id": room.current_turn_user_id(),
            "party": room_core.party_status(room),
        }, status=409)
    except Exception as e:
        logger.exception("Gemini error in room action")
        error_key = "content_blocked" if isinstance(e, gemini_client.ContentBlockedError) else "gemini_error"
        return web.json_response({
            "error": error_key,
            "detail": str(e),
            "options": room.pending_options,
            "character": seat.character,
            "active_user_id": room.current_turn_user_id(),
            "party": room_core.party_status(room),
        }, status=502)

    if roll_info:
        result["roll"] = roll_info

    rooms.touch_seen(room.room_id, user_id)
    next_user_id = result.get("active_user_id")
    if next_user_id is not None and next_user_id != user_id:
        await _notify_turn(room, next_user_id)

    return web.json_response(result)


@routes.post("/api/room/leave")
async def api_room_leave(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    user_id = _authed_user_id(body)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    room_id = rooms.room_id_for_user(user_id)
    if room_id is None:
        return web.json_response({"error": "no_active_room"}, status=404)

    rooms.leave_room(room_id, user_id)
    return web.json_response({"ok": True})


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app
