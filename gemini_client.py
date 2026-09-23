"""Thin wrapper around the Gemini API for generating DnD-style narration.

Uses the free-tier-friendly gemini-3.5-flash-lite model by default (the 2.5
line was retired for new users in late 2026); swap GEMINI_MODEL if you want
richer prose from gemini-3.6-flash instead.
"""

import os
import re

import google.generativeai as genai

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

DEFAULT_HP = 30

# Matches a trailing "[STATE:HP=18/30;MONEY=45;INV=rope, sword]" tag the
# model is instructed to always append. MONEY and INV are optional.
STATE_TAG_RE = re.compile(r"\[STATE:(.+?)\]\s*$", re.DOTALL)

# Marks a numbered option as requiring a dice roll, independent of narration
# language (like the STATE tag, this literal token is always in English).
ROLL_MARKER = "[ROLL]"

SYSTEM_PROMPT = """\
You are an experienced, vivid Dungeon Master running an interactive text \
RPG adventure.

Follow these rules on every reply:

1. LANGUAGE: Always write your entire reply in the language given by the \
   "Respond in:" instruction found in the user's message. Never mix \
   languages. Two exceptions, always kept literally in English regardless \
   of narration language: the "[STATE:...]" tag (rule 9) and the "[ROLL]" \
   marker (rule 7).

2. NARRATIVE VOICE: Vary your prose. Do not open every sentence with the \
   same second-person pronoun ("you"/"ти"/"ty" etc.) — use the grammar of \
   the target language naturally (many languages can drop the pronoun \
   entirely via verb conjugation). From time to time, instead of plain \
   narration, address the character directly by name in a punchy, vivid, \
   slightly dramatic aside that comments on what they just did — for \
   example (English, adapt the style and language): "Kyr Volt, you just \
   handed Arasaka an entire heist on a silver platter — and they even \
   brought their own timer. Seven minutes until the V-11 package: either \
   you're a genius, or a very stylish criminal with terrible planning." \
   Don't overuse this — a couple of times per adventure is plenty, keep \
   most narration natural and grounded.

3. Briefly (3-6 sentences) describe the outcome of the player's last action \
   and the current scene.

4. DICE OUTCOME TIERS: if the player's message includes a roll result, it \
   will contain one of these English tier keywords: critical_failure, \
   failure, partial_success, success, critical_success. Interpret them like \
   this and reflect it clearly in the narration:
   - critical_failure: things go wrong AND there's an extra complication.
   - failure: the action simply doesn't work, no extra twist needed.
   - partial_success: it works, but with a cost, complication, or catch.
   - success: it works cleanly.
   - critical_success: it works exceptionally well, with a bonus.
   Never invent your own dice roll — only use a result explicitly given to \
   you.

5. CHARACTER STATE MATTERS: consider the character's current HP, money, \
   inventory, and any noted physical/mental traits (all shown in the \
   character sheet / STATE below) when deciding what's realistic. Low funds \
   should block bribes/purchases they can't afford; missing the right item \
   should make certain approaches fail or need a workaround; a character \
   described as physically weak or unskilled at something should find \
   matching tasks harder, and vice versa for their strengths. Let \
   inventory and money change believably through the story (loot, cost of \
   supplies, payment for services, etc.).

6. HP PACING: keep the adventure's pace long and forgiving. Most actions, \
   even failed ones, should NOT cause damage at all — only apply damage \
   when the fiction clearly involves real physical danger, and even then \
   keep it small (typically 5-15% of max HP for a normal hit, more only for \
   a rare, clearly telegraphed severe threat). Do not let HP drop to 0 \
   except after many turns of accumulated, ignored danger. If wounded \
   (HP below ~45%) or critically wounded (below ~20%), reflect this mildly \
   in narration without shutting down the player's options.

7. OPTIONS: always end with a numbered list of 2-4 action options ("1)", \
   "2)", etc). If an option meaningfully depends on luck or skill and should \
   require a dice roll, append the literal token "{roll_marker}" at the very \
   end of that option's line (after the text, before the newline) — do not \
   translate or explain this token, just append it exactly as shown, and \
   only on options that truly warrant a roll (not every option needs one).

8. Keep the tone adventurous, not overly grim, with no graphic violence or \
   disallowed content.

9. STATE TAG: the VERY LAST LINE of your reply must always be exactly one \
   tag in this exact form (English keys, literal brackets/semicolons):
   [STATE:HP=current/max;MONEY=amount;INV=item, item, item]
   - HP is mandatory (current/max as integers).
   - MONEY is the character's current funds as a short label (e.g. "45" or \
     "45 credits" or "12 gold") — include it whenever the world/character \
     has an established currency; omit only if truly not applicable yet.
   - INV is a short comma-separated list of notable carried items — keep it \
     concise (a handful of items, not a huge inventory dump).
   Never omit HP. Never explain this tag. Never put anything after it.
""".format(roll_marker=ROLL_MARKER)


def _configure():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    genai.configure(api_key=api_key)


_model = None


def get_model():
    global _model
    if _model is None:
        _configure()
        _model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=SYSTEM_PROMPT,
        )
    return _model


def parse_state_tag(text: str) -> tuple[str, dict]:
    """Strip the trailing [STATE:...] tag and return (clean_text, state).
    state may contain keys: hp, max_hp, money, inventory — any of them can
    be missing if the model omitted that part."""
    match = STATE_TAG_RE.search(text.strip())
    if not match:
        return text.strip(), {}

    state: dict = {}
    for part in match.group(1).split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if key == "HP" and "/" in value:
            hp_str, max_str = value.split("/", 1)
            try:
                state["hp"] = int(hp_str.strip())
                state["max_hp"] = int(max_str.strip())
            except ValueError:
                pass
        elif key == "MONEY" and value:
            state["money"] = value
        elif key == "INV" and value:
            state["inventory"] = value

    clean = STATE_TAG_RE.sub("", text).strip()
    return clean, state


def _status_note(character: dict) -> str:
    hp = character.get("hp")
    max_hp = character.get("max_hp")
    if hp is None or not max_hp:
        return ""
    ratio = hp / max_hp
    if ratio <= 0.2:
        return "STATUS: character is critically wounded — reflect this mildly in narration, but they still have real options."
    if ratio <= 0.45:
        return "STATUS: character is wounded — reflect this mildly in narration."
    return ""


def build_context(summary: str, recent_turns: list[str], character: dict) -> str:
    parts = []
    if character:
        parts.append(f"Character sheet: {character}")
    note = _status_note(character)
    if note:
        parts.append(note)
    if summary:
        parts.append(f"Story summary so far: {summary}")
    if recent_turns:
        parts.append("Recent exchanges:\n" + "\n".join(recent_turns))
    return "\n\n".join(parts)


def generate_story_turn(
    summary: str,
    recent_turns: list[str],
    character: dict,
    player_input: str,
    language_name: str,
) -> str:
    context = build_context(summary, recent_turns, character)
    prompt = (
        f"Respond in: {language_name}.\n\n"
        f"{context}\n\nPlayer's action now: {player_input}"
    )
    model = get_model()
    response = model.generate_content(prompt)
    return response.text.strip()


def generate_new_adventure_opening(
    category_label: str,
    category_hint: str,
    world_description: str | None,
    character_brief: str,
    language_name: str,
) -> str:
    """Generate the opening scene for a brand-new adventure.

    category_label/category_hint come from world_categories.py.
    world_description is either the player's own text or None (Gemini invents one).
    character_brief is a ready-made instruction describing the character —
    either the player's own description or gender/age for random generation.
    """
    if world_description:
        world_part = f"World description from the player: {world_description}"
    else:
        world_part = f"Invent a world yourself that fits this category ({category_hint})."

    prompt = (
        f"Respond in: {language_name}.\n\n"
        f"World category: {category_label} ({category_hint}).\n"
        f"{world_part}\n\n"
        f"{character_brief}\n\n"
        f"Also invent, fitting the world and character: a short note of their physical and "
        "mental abilities/traits (e.g. strong but slow-witted, frail but clever — keep it brief "
        "and include it naturally in the opening description), some starting money in a currency "
        "that fits the world, and 2-4 starting inventory items. Reflect all of this in the "
        f"closing STATE tag. Start at full health: HP={DEFAULT_HP}/{DEFAULT_HP} unless the "
        "character description implies a different max HP, in which case use that.\n\n"
        "Begin a new short adventure: describe the setting, the hook, and the opening scene "
        "with the character, then the list of action options."
    )
    model = get_model()
    response = model.generate_content(prompt)
    return response.text.strip()


def generate_summary(existing_summary: str, recent_turns: list[str], language_name: str) -> str:
    """Compress the previous summary plus recent turns into one short,
    updated summary — called periodically to keep long-term context (money,
    inventory, plot threads, relationships) alive without an ever-growing
    prompt."""
    prompt = (
        f"Respond in: {language_name}.\n\n"
        "Summarize this adventure so far in 3-5 concise sentences. Preserve important "
        "ongoing facts: key plot points, relationships, notable injuries, and anything about "
        "the character's money or inventory that matters going forward. Combine the previous "
        "summary with the recent events below into one updated summary — do not just append, "
        "actually condense.\n\n"
        f"Previous summary: {existing_summary or '(none yet)'}\n\n"
        "Recent events:\n" + "\n".join(recent_turns)
    )
    model = get_model()
    response = model.generate_content(prompt)
    return response.text.strip()
