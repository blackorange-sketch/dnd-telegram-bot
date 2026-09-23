"""Core game-turn logic, independent of delivery channel (Telegram bot
messages vs. the Mini App's HTTP API). Both call into this module so the
actual game rules live in exactly one place."""

import logging
import re

import dice
import gemini_client
import languages as lang
import ui_strings as ui
from game_state import SUMMARY_EVERY_N_TURNS, GameState, save_state

logger = logging.getLogger(__name__)

OPTION_LINE_RE = re.compile(r"^\s*\d\)\s*.+$", re.MULTILINE)


def format_options(story_text: str, lang_key: str) -> tuple[str, list[dict]]:
    """Find numbered option lines, strip the internal [ROLL] marker and
    replace it with a localized hint for display, and return the cleaned
    text plus a list of {"text": ..., "requires_roll": bool} in order."""
    options: list[dict] = []

    def repl(match: re.Match) -> str:
        line = match.group(0)
        requires_roll = gemini_client.ROLL_MARKER in line
        clean_line = line.replace(gemini_client.ROLL_MARKER, "").rstrip()
        text_only = re.sub(r"^\s*\d\)\s*", "", clean_line)
        options.append({"text": text_only, "requires_roll": requires_roll})
        if requires_roll:
            clean_line += " " + ui.t(lang_key, "requires_roll_hint")
        return clean_line

    new_text = OPTION_LINE_RE.sub(repl, story_text)
    return new_text, options


async def maybe_summarize(game: GameState):
    """Every SUMMARY_EVERY_N_TURNS turns, compress recent_turns into the
    running summary so long adventures don't lose track of money,
    inventory, and plot threads once old turns get dropped."""
    if game.turn_count == 0 or game.turn_count % SUMMARY_EVERY_N_TURNS != 0:
        return
    try:
        new_summary = gemini_client.generate_summary(
            existing_summary=game.summary,
            recent_turns=game.recent_turns,
            language_name=lang.prompt_name_for(game.language),
        )
        game.summary = new_summary
        game.recent_turns = game.recent_turns[-2:]
    except Exception:
        logger.exception("Summary generation failed, skipping this round")


def compute_roll(game: GameState, sides: int = 20) -> dict:
    """Roll a die, applying the HP-based penalty, and classify the outcome
    tier. Returns a plain dict so it's easy to send over the API too."""
    hp = game.character.get("hp", gemini_client.DEFAULT_HP)
    max_hp = game.character.get("max_hp", gemini_client.DEFAULT_HP)
    penalty = dice.hp_penalty(hp, max_hp)
    result = dice.roll(sides, modifier=penalty)
    tier = dice.classify(result.value, result.total)
    return {
        "sides": sides,
        "value": result.value,
        "total": result.total,
        "penalty": penalty,
        "tier": tier,
    }


def roll_action_text(roll_info: dict) -> str:
    """The English, language-independent action text fed to Gemini to
    describe a roll that already happened (see gemini_client's tier rules)."""
    return f"roll result: {roll_info['tier']} (natural {roll_info['value']}, total {roll_info['total']})"


async def perform_turn(game: GameState, player_input: str) -> dict:
    """Advance the story by one turn: call Gemini, parse the updated state,
    save the game, and return a plain dict describing the result. Raises
    whatever exception Gemini raised on failure (caller decides how to
    surface it) — the game is NOT mutated/saved in that case."""
    lang_key = game.language
    game.add_turn(f"[Player]: {player_input}")

    story_text = gemini_client.generate_story_turn(
        summary=game.summary,
        recent_turns=game.recent_turns,
        character=game.character,
        player_input=player_input,
        language_name=lang.prompt_name_for(lang_key),
    )

    clean_text, parsed_state = gemini_client.parse_state_tag(story_text)
    if "hp" in parsed_state:
        # max_hp shouldn't drift turn to turn — lock it once set, so a
        # model slip can't silently reset the character's max health.
        if not game.character.get("max_hp"):
            game.character["max_hp"] = parsed_state.get("max_hp")
        max_hp = game.character.get("max_hp") or parsed_state["hp"]
        game.character["hp"] = max(0, min(parsed_state["hp"], max_hp))
    if "money" in parsed_state:
        game.character["money"] = parsed_state["money"]
    if "inventory" in parsed_state:
        game.character["inventory"] = parsed_state["inventory"]

    display_text, options = format_options(clean_text, lang_key)

    game.add_turn(f"[DM]: {clean_text}")
    game.pending_options = options
    game.turn_count += 1
    await maybe_summarize(game)
    save_state(game)

    hp = game.character.get("hp")
    return {
        "text": display_text,
        "options": options,
        "character": game.character,
        "defeated": hp is not None and hp <= 0,
    }
