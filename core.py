"""Core game-turn logic, independent of delivery channel (Telegram bot
messages vs. the Mini App's HTTP API). Both call into this module so the
actual game rules live in exactly one place."""

import logging
import re

import dice
import gemini_client
import languages as lang
import world_categories as wc
from game_state import SUMMARY_EVERY_N_TURNS, GameState, delete_state, load_state, save_state

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
        new_summary = await gemini_client.generate_summary(
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
    extra = f", modifier {int(roll_info['modifier']):+d} from {roll_info['attribute']}" if roll_info.get("attribute") else ""
    return f"roll result: {roll_info['tier']} (natural {roll_info['value']}, total {roll_info['total']}{extra})"


def _extract_number(value) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(\.\d+)?", str(value))
    return float(match.group()) if match else None


def normalize_inventory(items) -> list[dict]:
    """Coerce an inventory list into the current {name, equipped} shape,
    whether it comes fresh from a Gemini function call (already shaped this
    way) or from an older save/legacy text-tag parse (a plain list of
    strings). Drops anything without a usable name."""
    normalized: list[dict] = []
    for item in items or []:
        if isinstance(item, dict):
            name = item.get("name")
            if not name:
                continue
            normalized.append({"name": str(name), "equipped": bool(item.get("equipped", False))})
        elif item:
            normalized.append({"name": str(item), "equipped": False})
    return normalized


def _find_existing_key(existing: dict, name: str) -> str:
    """Return the key in `existing` that `name` most plausibly refers to, or
    `name` itself if it looks genuinely new. Safety net on top of the prompt
    (which already tells the model to reuse exact names): catches case/
    punctuation differences, one name containing the other ("Marta" vs
    "Marta the innkeeper"), and near-identical spellings, so the journal
    doesn't grow a second entry for the same NPC/quest."""
    import difflib

    def norm(x: str) -> str:
        return " ".join("".join(c if c.isalnum() else " " for c in x.casefold()).split())

    target = norm(name)
    if not target:
        return name
    best, best_score = None, 0.0
    for key in existing:
        k = norm(key)
        if not k:
            continue
        if k == target:
            return key
        score = difflib.SequenceMatcher(None, k, target).ratio()
        shorter, longer = sorted((k, target), key=len)
        if len(shorter) >= 4 and (f" {shorter} " in f" {longer} "):
            score = max(score, 0.9)
        if score > best_score:
            best, best_score = key, score
    return best if best is not None and best_score >= 0.82 else name


def apply_journal_update(target, journal: dict) -> None:
    """Merge an update_journal function-call result into a GameState or
    RoomState (both expose the same .location/.visited_locations/.npcs/
    .quests attributes). Only the fields Gemini actually reported this turn
    are touched — see gemini_client.py's SYSTEM_PROMPT rule 13, which tells
    the model to omit anything that didn't change."""
    if not journal:
        return

    location = journal.get("location")
    if location and location.get("name"):
        name = str(location["name"])
        description = str(location.get("description") or "")
        target.location = {"name": name, "description": description}
        if name not in target.visited_locations:
            target.visited_locations.append(name)
        # Builds the schematic map: keep every visited location's description
        # and which already-known location it was reached from (reported at
        # most once, the turn it's first discovered — see SYSTEM_PROMPT rule
        # 13 and UPDATE_JOURNAL_FUNCTION's connected_from). A revisit later
        # only updates the description, never overwrites a real
        # connected_from with a missing one.
        node = dict(target.location_graph.get(name, {}))
        if description:
            node["description"] = description
        connected_from = location.get("connected_from")
        if connected_from and not node.get("connected_from"):
            node["connected_from"] = str(connected_from)
        target.location_graph[name] = node

    for npc in journal.get("npcs") or []:
        name = npc.get("name") if isinstance(npc, dict) else None
        if not name:
            continue
        name = _find_existing_key(target.npcs, str(name))
        existing = target.npcs.get(name, {})
        target.npcs[name] = {
            "description": npc.get("description", existing.get("description", "")),
            "relationship": npc.get("relationship", existing.get("relationship", "")),
        }

    for quest in journal.get("quests") or []:
        title = quest.get("title") if isinstance(quest, dict) else None
        if not title:
            continue
        title = _find_existing_key(target.quests, str(title))
        existing = target.quests.get(title, {})
        target.quests[title] = {
            "status": quest.get("status", existing.get("status", "active")),
            "description": quest.get("description", existing.get("description", "")),
        }


def journal_payload(target) -> dict:
    """Shape a GameState/RoomState's journal fields for the HTTP API /
    frontend: dicts become name-keyed lists so the client doesn't need to
    know the storage representation."""
    return {
        "location": target.location,
        "visited_locations": target.visited_locations,
        "location_graph": [
            {"name": name, **info} for name, info in target.location_graph.items()
        ],
        "npcs": [{"name": name, **info} for name, info in target.npcs.items()],
        "quests": [{"title": title, **info} for title, info in target.quests.items()],
    }


def compute_changes(prev_hp, prev_money, prev_inventory: list[dict], character: dict) -> dict:
    """Diff the character's state before/after a turn into a compact
    {hp_delta, money_delta, inventory_added, inventory_removed} dict, so
    the player can see at a glance what just changed. prev_inventory and
    the character's current inventory are both {name, equipped} dicts
    (see normalize_inventory); items are compared by name."""
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
    prev_names = {item["name"] for item in prev_inventory}
    new_names = {item["name"] for item in new_inventory}
    added = [item["name"] for item in new_inventory if item["name"] not in prev_names]
    removed = [item["name"] for item in prev_inventory if item["name"] not in new_names]
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
        parts.append(f"❤️ {int(hp_delta):+d}")
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
    prev_inventory = normalize_inventory(game.character.get("inventory"))

    category_key = game.character.get("category")
    category_hint = wc.short_hint_for(category_key) if category_key else None
    current_location_name = (game.location or {}).get("name")

    try:
        clean_text, parsed_state, journal = await gemini_client.generate_story_turn(
            summary=game.summary,
            recent_turns=game.recent_turns,
            character=game.character,
            player_input=player_input,
            language_name=lang.prompt_name_for(lang_key),
            category_hint=category_hint,
            current_location_name=current_location_name,
            known_npcs=game.npcs,
            known_quests=game.quests,
        )
        used_last_resort = False
    except gemini_client.ContentBlockedError:
        logger.warning("Turn blocked by safety filter, retrying once with a softened prompt")
        try:
            clean_text, parsed_state, journal = await gemini_client.generate_story_turn(
                summary=game.summary,
                recent_turns=game.recent_turns,
                character=game.character,
                player_input=player_input,
                language_name=lang.prompt_name_for(lang_key),
                category_hint=category_hint,
                current_location_name=current_location_name,
                known_npcs=game.npcs,
                known_quests=game.quests,
                soften=True,
            )
            used_last_resort = False
        except gemini_client.ContentBlockedError:
            # The raw recent-turn history itself (not just the new action) is
            # part of every prompt — if something graphic landed there a few
            # turns back, it keeps getting re-sent and can keep tripping the
            # filter no matter what the player picks next. Last resort: drop
            # that raw history and lean on the compressed summary instead.
            logger.warning("Still blocked after softening; retrying once more without raw recent-turn history")
            clean_text, parsed_state, journal = await gemini_client.generate_story_turn(
                summary=game.summary,
                recent_turns=[],
                character=game.character,
                player_input=player_input,
                language_name=lang.prompt_name_for(lang_key),
                category_hint=category_hint,
                current_location_name=current_location_name,
                known_npcs=game.npcs,
                known_quests=game.quests,
                soften=True,
            )
            used_last_resort = True

    if "hp" in parsed_state:
        # max_hp shouldn't drift turn to turn — lock it once set, so a
        # model slip can't silently reset the character's max health.
        hp_val = int(parsed_state["hp"])
        if not game.character.get("max_hp"):
            game.character["max_hp"] = int(parsed_state.get("max_hp") or hp_val)
        max_hp = int(game.character.get("max_hp") or hp_val)
        game.character["hp"] = max(0, min(hp_val, max_hp))
    if "money" in parsed_state:
        game.character["money"] = parsed_state["money"]
    if "inventory" in parsed_state:
        game.character["inventory"] = normalize_inventory(parsed_state["inventory"])

    apply_journal_update(game, journal)

    changes = compute_changes(prev_hp, prev_money, prev_inventory, game.character)

    display_text, options = format_options(clean_text, lang_key)

    if used_last_resort:
        # The dropped history was the likely culprit — don't carry it
        # forward into future turns, or every subsequent turn would need
        # the same 3-tier fallback again. Keep just this exchange.
        game.recent_turns = [f"[Player]: {player_input}"]

    # Store the cleaned narration (options already stripped out), not the
    # raw clean_text — otherwise the numbered option lines end up duplicated
    # in both the buttons and the scrollable turn history/log.
    game.add_turn(f"[DM]: {display_text}")
    game.pending_options = options
    game.turn_count += 1
    await maybe_summarize(game)

    hp = game.character.get("hp")
    defeated = hp is not None and hp <= 0
    epilogue = None
    if defeated:
        # The adventure ends here: no more options, and a short dedicated
        # closing passage instead of just trailing off after the fatal hit.
        try:
            epilogue = await gemini_client.generate_epilogue(
                summary=game.summary,
                recent_turns=game.recent_turns,
                character=game.character,
                language_name=lang.prompt_name_for(lang_key),
                category_hint=category_hint,
                reason="defeated",
            )
            game.add_turn(f"[DM]: {epilogue}")
        except Exception:
            logger.exception("Epilogue generation failed; ending without one")
        options = []
        game.pending_options = []
        save_state(game)
        delete_state(game.user_id)
    else:
        save_state(game)

    return {
        "text": display_text,
        "options": options,
        "character": game.character,
        "defeated": defeated,
        "epilogue": epilogue,
        "log": game.full_log,
        "changes": changes,
        "changes_line": format_changes_line(changes),
        "journal": journal_payload(game),
    }


def build_character_brief(
    character_description: str | None, gender: str | None, age: str | None
) -> tuple[str, dict]:
    """Shared by solo and multiplayer character creation: returns the
    instruction text to give Gemini plus the starting character_record
    (before hp/money/inventory/attributes are filled in from its reply)."""
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
    return character_brief, character_record


async def create_adventure(
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
    character_brief, character_record = build_character_brief(character_description, gender, age)

    try:
        clean_text, parsed_state, attrs, journal = await gemini_client.generate_new_adventure_opening(
            category_label=wc.label_for(category_key, language_key),
            category_hint=wc.hint_for(category_key),
            world_description=world_description,
            character_brief=character_brief,
            language_name=language_name,
        )
    except gemini_client.ContentBlockedError:
        logger.warning("Opening blocked by safety filter, retrying once with a softened prompt")
        clean_text, parsed_state, attrs, journal = await gemini_client.generate_new_adventure_opening(
            category_label=wc.label_for(category_key, language_key),
            category_hint=wc.hint_for(category_key),
            world_description=world_description,
            character_brief=character_brief,
            language_name=language_name,
            soften=True,
        )

    hp = int(parsed_state.get("hp", gemini_client.DEFAULT_HP))
    max_hp = int(parsed_state.get("max_hp", gemini_client.DEFAULT_HP))

    character_record["category"] = category_key
    character_record["world_description"] = world_description
    character_record["hp"] = hp
    character_record["max_hp"] = max_hp
    if parsed_state.get("name"):
        character_record["name"] = parsed_state["name"]
    if "money" in parsed_state:
        character_record["money"] = parsed_state["money"]
    if "inventory" in parsed_state:
        character_record["inventory"] = normalize_inventory(parsed_state["inventory"])
    if attrs:
        character_record["attributes"] = attrs

    display_text, options = format_options(clean_text, language_key)

    game = GameState(user_id=user_id, character=character_record, language=language_key)
    apply_journal_update(game, journal)
    game.add_turn(f"[DM]: {display_text}")
    game.pending_options = options
    save_state(game)

    return {
        "text": display_text,
        "options": options,
        "character": game.character,
        "defeated": False,
        "log": game.full_log,
        "journal": journal_payload(game),
    }


async def end_adventure_now(user_id: int) -> dict:
    """Used by the player's own 'End adventure' menu button: generate a
    short closing passage for wherever they left off, then delete the save
    so the next game starts fresh. Returns {"epilogue": None} if there was
    no active game to end (nothing to do, caller just resets the UI)."""
    game = load_state(user_id)
    if game is None:
        return {"epilogue": None}

    category_key = game.character.get("category")
    category_hint = wc.short_hint_for(category_key) if category_key else None
    epilogue = None
    try:
        epilogue = await gemini_client.generate_epilogue(
            summary=game.summary,
            recent_turns=game.recent_turns,
            character=game.character,
            language_name=lang.prompt_name_for(game.language),
            category_hint=category_hint,
            reason="ended_by_player",
        )
    except Exception:
        logger.exception("Epilogue generation failed; ending without one")

    delete_state(user_id)
    return {"epilogue": epilogue}
