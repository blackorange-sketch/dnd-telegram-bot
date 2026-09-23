"""HTTP side of the bot: serves the Mini App's static page and a small
JSON API it calls (/api/state, /api/action). Runs in the same process as
the Telegram polling bot — see main.py's `main()`.
"""

import logging
import os
from pathlib import Path

from aiohttp import web

import core
import gemini_client
from game_state import load_state
from telegram_auth import verify_init_data

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
STATIC_DIR = Path(__file__).parent / "static"

routes = web.RouteTableDef()


def _authed_user_id(body: dict) -> int | None:
    init_data = body.get("initData", "")
    user = verify_init_data(init_data, BOT_TOKEN)
    if not user:
        return None
    return user.get("id")


def _last_dm_text(game) -> str:
    for turn in reversed(game.recent_turns):
        if turn.startswith("[DM]:"):
            return turn[len("[DM]:"):].strip()
    return ""


@routes.get("/")
@routes.get("/miniapp")
async def serve_miniapp(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


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
    })


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
            roll_info = core.compute_roll(game)
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
        return web.json_response({"error": "gemini_error", "detail": str(e)}, status=502)

    if roll_info:
        result["roll"] = roll_info
    return web.json_response(result)


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app
