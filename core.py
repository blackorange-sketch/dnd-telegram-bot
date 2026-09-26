"""Core game-turn logic, independent of delivery channel (Telegram bot
messages vs. the Mini App's HTTP API). Both call into this module so the
actual game rules live in exactly one place."""

import logging
import re

import dice
import gemini_client
import languages as lang
import world_categories as wc
from game_state import SUMMARY_EVERY_N_TURNS, GameState, save_state

logger = logging.getLogger(__name__)

# Accept "1)" (the format we ask for) as well as "1." (a drift Gemini
# sometimes produces anyway) so a formatting slip doesn't break parsing.
OPTION_LINE_RE = re.compile(r"^\s*\d[.)]\s*.+$", re.MULTILINE)


def format_options(story_text: str, lang_key: str) -> tuple[str, list[dict]]:
    """Find numbered option lines and pull them out into a list of
    {"text": ..., "requires_roll": bool, "attribute": str|None} (for
    rendering as buttons), and return the narrative with those lines
    removed entirely — options are meant to live only in the buttons, not
    duplicated in the story text."""
    options: list[dict] = []

    def repl(match: re.Match) -> str:
        line = match.group(0)
        requires_roll = gemini_client.ROLL_MARKER in line
        attr_match = gemini_client.ATTR_MARKER_RE.search(line)
        attribute = attr_match.group(1).strip() if attr_match else None
        clean_line = line.replace(gemini_client.ROLL_MARKER, "")
        clean_line = gemini_client.ATTR_MARKER_RE.sub("", clean_line).rstrip()
        text_only = re.sub(r"^\s*\d[.)]\s*", "", clean_line)
        options.append({"text": text_only, "requires_roll": requires_roll, "attribute": attribute})
        return ""

    new_text = OPTION_LINE_RE.sub(repl, story_text)
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip()
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


def compute_modifier(character: dict, attribute: str | None = None) -> int:
    """The modifier a roll would get right now: HP-based penalty, plus a
    bonus/penalty from a linked attribute if one applies. Used both to
    preview a modifier on a button (before rolling) and to actually apply
    it when the roll happens, so the two always agree."""
    hp = character.get("hp", gemini_client.DEFAULT_HP)
    max_hp = character.get("max_hp", gemini_client.DEFAULT_HP)
    modifier = dice.hp_penalty(hp, max_hp)
    if attribute:
        value = (character.get("attributes") or {}).get(attribute)
        if value is not None:
            modifier += dice.attribute_modifier(value)
    return modifier


def compute_roll(game: GameState, sides: int = 20, attribute: str | None = None) -> dict:
    """Roll a die, applying the HP-based penalty and any linked-attribute
    modifier, and classify the outcome tier. Returns a plain dict so it's
    easy to send over the API too."""
    modifier = compute_modifier(game.character, attribute)
    result = dice.roll(sides, modifier=modifier)
    tier = dice.classify(result.value, result.total)
    return {
        "sides": sides,
        "value": result.value,
        "total": result.total,
        "modifier": modifier,
        "attribute": attribute,
        "tier": tier,
    }


def roll_action_text(roll_info: dict) -> str:
    """The English, language-independent action text fed to Gemini to
    describe a roll that already happened (see gemini_client's tier rules)."""
    extra = f", modifier {roll_info['modifier']:+d} from {roll_info['attribute']}" if roll_info.get("attribute") else ""
    return f"roll result: {roll_info['tier']} (natural {roll_info['value']}, total {roll_info['total']}{extra})"


def _extract_number(value) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(\.\d+)?", str(value))
    return float(match.group()) if match else None


def compute_changes(prev_hp, prev_money, prev_inventory: list[str], character: dict) -> dict:
    """Diff the character's state before/after a turn into a compact
    {hp_delta, money_delta, inventory_added, inventory_removed} dict, so
    the player can see at a glance what just changed."""
    changes: dict = {}

    new_hp = character.get("hp")
    if prev_hp is not None and new_hp is not None and new_hp != prev_hp:
        changes["hp_delta"] = new_hp - prev_hp

    new_money = character.get("money")
    prev_num = _extract_number(prev_money)
    new_num = _extract_number(new_money)
    if prev_num is not None and new_num is not None and new_num != prev_num:
        delta = new_num - prev_num
        changes["money_delta"] = int(delta) if delta == int(delta) else delta
    elif new_money and new_money != prev_money and prev_money is not None:
        changes["money_note"] = new_money

    new_inventory = character.get("inventory") or []
    added = [item for item in new_inventory if item not in prev_inventory]
    removed = [item for item in prev_inventory if item not in new_inventory]
    if added:
        changes["inventory_added"] = added
    if removed:
        changes["inventory_removed"] = removed

    return changes


def format_changes_line(changes: dict) -> str:
    """A compact, mostly-language-independent line like '❤️ -5   💰 +15
    🎒 +sword, -torch' summarizing compute_changes()'s output."""
    parts = []
    hp_delta = changes.get("hp_delta")
    if hp_delta:
        parts.append(f"❤️ {hp_delta:+d}")
    money_delta = changes.get("money_delta")
    if money_delta:
        parts.append(f"💰 {money_delta:+g}")
    elif changes.get("money_note"):
        parts.append(f"💰 {changes['money_note']}")
    added = changes.get("inventory_added") or []
    removed = changes.get("inventory_removed") or []
    if added:
        parts.append("🎒 +" + ", ".join(added))
    if removed:
        parts.append("🎒 -" + ", ".join(removed))
    return "   ".join(parts)


async def perform_turn(game: GameState, player_input: str) -> dict:
    """Advance the story by one turn: call Gemini, parse the updated state,
    save the game, and return a plain dict describing the result. Raises
    whatever exception Gemini raised on failure (caller decides how to
    surface it) — the game is NOT mutated/saved in that case."""
    lang_key = game.language
    game.add_turn(f"[Player]: {player_input}")

    prev_hp = game.character.get("hp")
    prev_money = game.character.get("money")
    prev_inventory = list(game.character.get("inventory") or [])

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

    changes = compute_changes(prev_hp, prev_money, prev_inventory, game.character)

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
        "log": game.full_log,
        "changes": changes,
        "changes_line": format_changes_line(changes),
    }


def create_adventure(
    user_id: int,
    language_key: str,
    category_key: str,
    world_description: str | None,
    character_description: str | None,
    gender: str | None,
    age: str | None,
) -> dict:
    """Generate the opening scene for a brand-new adventure, save a fresh
    GameState for `user_id`, and return the same shape perform_turn() does
    (so both the bot and the Mini App can render either result the same
    way). Raises on a Gemini failure — nothing is saved in that case."""
    language_name = lang.prompt_name_for(language_key)

    if character_description:
        character_brief = (
            f"The player described the character like this: {character_description}. "
            "Use this description, filling in small extra details if needed (name, class, traits)."
        )
        character_record = {"description": character_description}
    else:
        gender = gender or "any"
        age = age or "any"
        character_brief = (
            f"Invent the character yourself: gender — {gender}, age — {age}. "
            "Make up a name, class/profession, and a short backstory that fits the world."
        )
        character_record = {"gender": gender, "age": age, "generated": True}

    opening = gemini_client.generate_new_adventure_opening(
        category_label=wc.label_for(category_key, language_key),
        category_hint=wc.hint_for(category_key),
        world_description=world_description,
        character_brief=character_brief,
        language_name=language_name,
    )

    text_after_state, parsed_state = gemini_client.parse_state_tag(opening)
    clean_text, attrs = gemini_client.parse_attrs_tag(text_after_state)
    hp = parsed_state.get("hp", gemini_client.DEFAULT_HP)
    max_hp = parsed_state.get("max_hp", gemini_client.DEFAULT_HP)

    character_record["category"] = category_key
    character_record["world_description"] = world_description
    character_record["hp"] = hp
    character_record["max_hp"] = max_hp
    if "money" in parsed_state:
        character_record["money"] = parsed_state["money"]
    if "inventory" in parsed_state:
        character_record["inventory"] = parsed_state["inventory"]
    if attrs:
        character_record["attributes"] = attrs

    display_text, options = format_options(clean_text, language_key)

    game = GameState(user_id=user_id, character=character_record, language=language_key)
    game.add_turn(f"[DM]: {clean_text}")
    game.pending_options = options
    save_state(game)

    return {
        "text": display_text,
        "options": options,
        "character": game.character,
        "defeated": False,
        "log": game.full_log,
    }
