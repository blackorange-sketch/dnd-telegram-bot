"""Thin wrapper around the Gemini API for generating DnD-style narration.

Uses the free-tier-friendly gemini-3.5-flash-lite model by default (the 2.5
line was retired for new users in late 2026); swap GEMINI_MODEL if you want
richer prose from gemini-3.6-flash instead.
"""

import os
import re

import google.generativeai as genai

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

DEFAULT_HP = 20

# Matches a trailing "[HP:12/20]" tag the model is instructed to always append.
HP_TAG_RE = re.compile(r"\[HP:\s*(\d+)\s*/\s*(\d+)\s*\]\s*$")

SYSTEM_PROMPT = """\
You are an experienced Dungeon Master running an interactive text RPG \
adventure.

Follow these rules on every reply:
1. Always write your entire reply in the language given by the "Respond in:" \
   instruction found in the user's message. Never mix languages.
2. Briefly (3-6 sentences) describe the outcome of the player's last action \
   and the current scene.
3. If a dice roll result is present in the user's message, use it to decide \
   success, failure, or a critical outcome (a natural 20 is a critical \
   success, a natural 1 is a critical failure). Never invent your own roll.
4. Track the character's HP (health). If the context shows the character is \
   wounded (HP below half) or critically wounded (HP below a quarter), this \
   MUST visibly affect the narration and the difficulty of the situation: \
   they move slower, are more vulnerable, may need to rest, retreat, or use \
   an item. Injuries have real narrative and tactical consequences on the \
   choices you offer next.
5. When an action in the story causes damage or healing, decide a reasonable \
   amount yourself and update the HP accordingly.
6. Always end with a numbered list of 2-4 action options ("1)", "2)", etc). \
   Occasionally mark an option as requiring a dice roll, e.g. \
   "(requires a d20 roll)".
7. Keep the tone adventurous, not overly grim, with no graphic violence or \
   disallowed content.
8. The VERY LAST LINE of your reply must always be exactly one tag in the \
   form [HP:current/max] reflecting the character's HP after this turn's \
   events (unchanged if nothing affected it this turn). Never omit this tag, \
   never explain it, never put anything after it.
"""


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


def parse_hp_tag(text: str) -> tuple[str, int | None, int | None]:
    """Strip the trailing [HP:x/y] tag from a reply and return
    (clean_text, hp, max_hp). hp/max_hp are None if the tag wasn't found."""
    match = HP_TAG_RE.search(text.strip())
    if not match:
        return text.strip(), None, None
    hp, max_hp = int(match.group(1)), int(match.group(2))
    clean = HP_TAG_RE.sub("", text).strip()
    return clean, hp, max_hp


def _status_note(character: dict) -> str:
    hp = character.get("hp")
    max_hp = character.get("max_hp")
    if hp is None or not max_hp:
        return ""
    ratio = hp / max_hp
    if ratio <= 0.25:
        return "STATUS: character is critically wounded — this must strongly limit their options."
    if ratio <= 0.5:
        return "STATUS: character is wounded — actions should be visibly harder."
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
        f"Start the character at full health: [HP:{DEFAULT_HP}/{DEFAULT_HP}] unless the "
        "character description implies a different starting max HP, in which case use that.\n\n"
        "Begin a new short adventure: describe the setting, the hook, and the opening scene "
        "with the character, then the list of action options."
    )
    model = get_model()
    response = model.generate_content(prompt)
    return response.text.strip()
