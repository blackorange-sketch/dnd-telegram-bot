"""Thin wrapper around the Gemini API for generating DnD-style narration.

Uses gemini-3.6-flash by default — noticeably better narration quality than
3.5 Flash-Lite for a bit more cost per call. Set GEMINI_MODEL to
"gemini-3.5-flash-lite" if you want the cheaper/faster tier instead.
"""

import asyncio
import json
import logging
import os
import re

import google.generativeai as genai
from google.generativeai.types import HarmBlockThreshold, HarmCategory

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

DEFAULT_HP = 30

# Loosen the categories relevant to a violent/dark adventure narrative so
# ordinary combat/peril description isn't over-eagerly blocked. Sexual
# content is deliberately left at Google's default (stricter) threshold —
# this bot allows brutal violence, not explicit sexual content.
SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
}


class ContentBlockedError(RuntimeError):
    """Raised when Gemini's safety filters blocked a response entirely
    (empty candidates) — a clear, catchable case distinct from other
    API/network failures."""


def _extract_text(response) -> str:
    if not response.candidates:
        reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", "unknown")
        raise ContentBlockedError(f"Response blocked by Gemini's safety filter (reason: {reason})")
    return response.text.strip()

# Matches the "[STATE]{"hp":18,"max_hp":30,...}[/STATE]" JSON block the model
# is instructed to append. Deliberately NOT anchored to the end of the text:
# in long games the model sometimes puts options after the tag, and an
# end-anchored pattern would then miss it and leak the raw tag to the player.
STATE_TAG_RE = re.compile(r"\[STATE\]\s*(\{.*?\})\s*\[/STATE\]", re.DOTALL)

# Matches an "[ATTRS]{"Strength":6,...}[/ATTRS]" JSON block, only expected
# once, right before the STATE tag, when a brand-new character is created.
ATTRS_TAG_RE = re.compile(r"\[ATTRS\]\s*(\{.*?\})\s*\[/ATTRS\]", re.DOTALL)

# Leftover from a truncated/garbled reply: an opening tag with NO matching
# close anywhere after it. Stripped defensively so raw tag text never reaches
# the player. The lookahead matters — a complete tag of the other kind (e.g.
# a valid [ATTRS] block while we're cleaning up after [STATE]) must survive.
DANGLING_TAG_RE = re.compile(
    r"\[STATE\](?!.*\[/STATE\]).*$|\[ATTRS\](?!.*\[/ATTRS\]).*$", re.DOTALL
)

# Marks a numbered option as requiring a dice roll, independent of narration
# language (like the STATE tag, this literal token is always in English).
ROLL_MARKER = "[ROLL]"

# An [ATTR:Label] marker right after [ROLL] ties that option's roll to one
# of the character's attributes (matched by exact label text).
ATTR_MARKER_OPEN = "[ATTR:"
ATTR_MARKER_CLOSE = "]"
ATTR_MARKER_RE = re.compile(r"\[ATTR:([^\]]+)\]")

SYSTEM_PROMPT = """\
You are an experienced, vivid Dungeon Master running an interactive text \
RPG adventure.

Follow these rules on every reply:

1. LANGUAGE: Always write your entire reply in the language given by the \
   "Respond in:" instruction found in the user's message. Never mix \
   languages. Two exceptions, always kept literally in English regardless \
   of narration language: the JSON keys inside the "[STATE]...[/STATE]" and \
   "[ATTRS]...[/ATTRS]" tags (rules 10-11) and the "[ROLL]" marker (rule 8). \
   String VALUES inside those JSON tags (item names, attribute labels) \
   should be written in the target language.

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

3. GENRE CONSISTENCY: when a "GENRE FLAVOR" block is given in the prompt, \
   lean into it every single turn, not just at the start — its vocabulary, \
   technology level, typical threats/factions, currency, and tone should \
   stay recognizable throughout the whole adventure. Don't let generic \
   fantasy-ish or contemporary-real-world phrasing drift in if the genre is \
   something else (e.g. no "gold coins" in a cyberpunk story, no casual \
   modern slang in a fantasy one). Reach for setting-appropriate sensory \
   detail (sounds, tech, architecture, clothing) that a reader would only \
   see in THIS genre, not a generic one.

4. Briefly (3-6 sentences) describe the outcome of the player's last action \
   and the current scene. When a scene naturally involves talking to an NPC, \
   bring it to life with a short line or two of actual spoken dialogue in \
   quotes (not every turn — only when a conversation is genuinely happening) \
   instead of just summarizing what was said.

5. DICE OUTCOME TIERS: if the player's message includes a roll result, it \
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

6. CHARACTER STATE MATTERS: consider the character's current HP, money, \
   inventory, and any noted physical/mental traits (all shown in the \
   character sheet / STATE below) when deciding what's realistic. Low funds \
   should block bribes/purchases they can't afford; missing the right item \
   should make certain approaches fail or need a workaround; a character \
   described as physically weak or unskilled at something should find \
   matching tasks harder, and vice versa for their strengths. Let \
   inventory and money change believably through the story (loot, cost of \
   supplies, payment for services, etc.).

7. HP PACING: keep the adventure's pace long and forgiving. Most actions, \
   even failed ones, should NOT cause damage at all — only apply damage \
   when the fiction clearly involves real physical danger, and even then \
   keep it small (typically 5-15% of max HP for a normal hit, more only for \
   a rare, clearly telegraphed severe threat). Do not let HP drop to 0 \
   except after many turns of accumulated, ignored danger. If wounded \
   (HP below ~45%) or critically wounded (below ~20%), reflect this mildly \
   in narration without shutting down the player's options.

8. OPTIONS: always end with a numbered list of 2-4 action options. Each \
   option MUST start with the digit followed by a closing parenthesis and a \
   space — exactly "1) ", "2) ", "3) " etc. Never use a period ("1.") or any \
   other format instead of the parenthesis; this exact punctuation is \
   required for the app to parse your options correctly. If an option \
   meaningfully depends on luck or skill and should require a dice roll, \
   append the literal token "{roll_marker}" at the very end of that \
   option's line (after the text, before the newline) — do not translate \
   or explain this token, just append it exactly as shown, and only on \
   options that truly warrant a roll (not every option needs one). If that \
   option's success ALSO meaningfully depends on one of the character's \
   attributes (shown in the character sheet in the context above), append \
   right after "{roll_marker}" the literal token "{attr_marker_open}Label{attr_marker_close}" \
   where Label is copied EXACTLY (same spelling/case) from one of the \
   character's existing attribute names — e.g. "...text... {roll_marker}[ATTR:Сила]". \
   Only add this when there's a genuine, obvious link (a strength-based \
   action to the strength attribute, a social action to a charisma-like \
   attribute, etc.) — most rolls don't need it.

9. TONE: this is a mature-rated (adult) game. Violence can be graphic and \
   visceral when the scene calls for it — real injuries, blood, brutal \
   combat, harsh consequences — don't sanitize danger into something toothless. \
   Dialogue can include profanity and coarse language where it genuinely fits \
   a character's voice (a hardened mercenary or a desperate criminal can \
   swear). That said: never sexualize or endanger a character described or \
   implied as a minor, under any framing. If a romantic or intimate moment \
   arises naturally in the story, keep it tasteful and non-explicit — build \
   the tension and atmosphere, then transition past the explicit act itself \
   ("fade to black") rather than describing it graphically.

10. STATE TAG: the VERY LAST LINE of your reply must always be exactly one \
   tag in this exact form — a single line, valid JSON, English keys, no \
   trailing commas, no comments:
   [STATE]{{"hp": current, "max_hp": max, "money": "amount label", "inventory": ["item", "item"]}}[/STATE]
   - Before writing it, check the "CURRENT STATE" given to you in the \
     prompt (when present) — it is the ground truth going into this turn. \
     Copy each value forward EXACTLY as given unless something in THIS \
     specific turn changed it (damage, healing, a purchase, a find, using \
     an item). Never reset, round, or reinvent hp/money/inventory from \
     scratch — always start from the given values and adjust only what \
     actually changed.
   - "hp" and "max_hp" are mandatory integers.
   - "money" is a short string label in the world's currency (e.g. "45" or \
     "45 credits" or "12 gold") — include it whenever the world/character \
     has an established currency; omit the key only if truly not \
     applicable yet.
   - "inventory" is a JSON array of short strings, one per notable item \
     (e.g. "rusty sword", "health potion x2"). KEEP IT LEAN: at most about \
     10 items. An item that is destroyed, broken beyond use, empty, or used \
     up must be REMOVED from the list — never keep it with a label like \
     "(destroyed)" or "(empty)". Merge duplicates into one entry with a \
     count ("energy cell x3"), and drop trivial junk. This pruning is \
     allowed on any turn, even when nothing else about the item changed. \
     The tag itself must stay compact — it is resent to you every turn.
   Never omit "hp"/"max_hp". Never explain this tag. Never put anything \
   after it.

11. ATTRS TAG (new characters only): if — and only if — the prompt \
    explicitly tells you this is a brand-new character being created, also \
    output one more JSON tag placed right before the STATE tag (same line \
    format rules apply):
    [ATTRS]{{"Label": value, "Label": value}}[/ATTRS]
    Pick 4-5 short attribute labels in the target language that fit the \
    world and character (e.g. physical strength, intellect, agility, \
    willpower — whatever suits the setting), each an integer from 1 to 10. \
    Never output this tag on ordinary story turns — attributes are set \
    once at character creation and stay fixed afterward.
""".format(roll_marker=ROLL_MARKER, attr_marker_open=ATTR_MARKER_OPEN, attr_marker_close=ATTR_MARKER_CLOSE)


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
            safety_settings=SAFETY_SETTINGS,
        )
    return _model


def _clean_after_tag_removal(text: str) -> str:
    """Drop any dangling half-tag (truncated/garbled reply) and tidy the
    blank lines a removed tag leaves behind."""
    text = DANGLING_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_state_tag(text: str) -> tuple[str, dict]:
    """Find the [STATE]{...}[/STATE] JSON tag ANYWHERE in the reply, strip it
    out, and return (clean_text, state). state may contain keys: hp, max_hp,
    money, inventory (a list) — any of them can be missing if the model
    omitted that part, and the dict is empty if there was no valid tag or the
    JSON was malformed. The tag is always removed from the text regardless,
    so raw tag text can never leak to the player."""
    matches = list(STATE_TAG_RE.finditer(text))
    state: dict = {}
    if matches:
        raw = matches[-1].group(1)  # if there are several, the last one wins
        try:
            data = json.loads(raw)
            hp, max_hp = data.get("hp"), data.get("max_hp")
            if isinstance(hp, int) and isinstance(max_hp, int):
                state["hp"] = hp
                state["max_hp"] = max_hp
            money = data.get("money")
            if isinstance(money, str) and money:
                state["money"] = money
            inventory = data.get("inventory")
            if isinstance(inventory, list):
                state["inventory"] = [str(item) for item in inventory]
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning("Failed to parse STATE tag JSON (%s): %r", e, raw)

    clean = _clean_after_tag_removal(STATE_TAG_RE.sub("", text))
    return clean, state


def parse_attrs_tag(text: str) -> tuple[str, dict | None]:
    """Find the [ATTRS]{...}[/ATTRS] JSON tag (only expected once, at
    character creation) anywhere in the reply, strip it out, and return
    (clean_text, attrs_dict_or_None)."""
    matches = list(ATTRS_TAG_RE.finditer(text))
    attrs = None
    if matches:
        raw = matches[-1].group(1)
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                attrs = {str(k): v for k, v in data.items()}
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning("Failed to parse ATTRS tag JSON (%s): %r", e, raw)

    clean = _clean_after_tag_removal(ATTRS_TAG_RE.sub("", text))
    return clean, attrs


STATE_FIELDS = {"hp", "max_hp", "money", "inventory"}

# Bookkeeping-only fields with zero value for the model to read every turn.
NOISE_FIELDS = {"generated"}

# Free-text fields that matter most once (at the opening, where they're used
# in full) — for ongoing turns they're truncated, since their substance is
# already reflected in the opening scene and carried forward via the running
# summary/recent turns.
TRUNCATE_FIELDS = {"description", "world_description"}
TRUNCATE_LENGTH = 220


def _background_dict(character: dict) -> dict:
    """Stable facts about the character (description, category, attributes,
    etc.) — everything except the fields that change turn to turn. Trimmed
    down for repeated per-turn use: this dict is resent on every single
    call, so anything static and not strictly needed stays out or gets
    shortened."""
    background = {
        k: v for k, v in character.items()
        if k not in STATE_FIELDS and k not in NOISE_FIELDS
    }
    for key in TRUNCATE_FIELDS:
        value = background.get(key)
        if isinstance(value, str) and len(value) > TRUNCATE_LENGTH:
            background[key] = value[:TRUNCATE_LENGTH].rstrip() + "…"
    return background


def _state_line(character: dict) -> str:
    """Render current HP/money/inventory as one explicit, unambiguous JSON
    line — kept separate from the character sheet dump and placed right
    before the player's action so the model can't lose track of it."""
    hp = character.get("hp")
    max_hp = character.get("max_hp")
    payload = {}
    if hp is not None and max_hp:
        payload["hp"] = hp
        payload["max_hp"] = max_hp
    else:
        payload["hp"] = DEFAULT_HP
        payload["max_hp"] = DEFAULT_HP
    if character.get("money"):
        payload["money"] = character["money"]
    if character.get("inventory"):
        payload["inventory"] = character["inventory"]
    return json.dumps(payload, ensure_ascii=False)


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
    background = _background_dict(character)
    if background:
        parts.append(f"Character background (stable, unrelated to current HP/money/items): {background}")
    note = _status_note(character)
    if note:
        parts.append(note)
    if summary:
        parts.append(f"Story summary so far: {summary}")
    if recent_turns:
        parts.append("Recent exchanges:\n" + "\n".join(recent_turns))
    return "\n\n".join(parts)


SOFTEN_NOTE = (
    "IMPORTANT: a previous attempt to generate this exact turn was blocked by "
    "the safety filter for being too graphic/extreme. For this attempt, "
    "significantly tone down violence, injury detail, and harsh language — "
    "keep the scene tense and consequential, but far less graphic — while "
    "still meaningfully advancing the story and following all other rules."
)


async def generate_story_turn(
    summary: str,
    recent_turns: list[str],
    character: dict,
    player_input: str,
    language_name: str,
    category_hint: str | None = None,
    party_note: str | None = None,
    soften: bool = False,
) -> str:
    context = build_context(summary, recent_turns, character)
    state_line = _state_line(character)
    genre_part = (
        f"GENRE FLAVOR (maintain this vocabulary, tone, technology level, and typical threats "
        f"consistently in this turn too, not just at the start): {category_hint}\n\n"
        if category_hint else ""
    )
    party_part = (
        f"PARTY (multiplayer — this is a shared adventure; acknowledge the other party members "
        f"as present/reacting where natural, but this turn's action and STATE tag belong only to "
        f"the acting player named below): {party_note}\n\n"
        if party_note else ""
    )
    prompt = (
        f"Respond in: {language_name}.\n\n"
        f"{genre_part}"
        f"{party_part}"
        f"{context}\n\n"
        f"CURRENT STATE (ground truth going into this turn — copy these exact values into "
        f"your closing [STATE:...] tag unless something in THIS turn explicitly changes them; "
        f"never invent or reset them): {state_line}\n\n"
        f"Player's action now: {player_input}"
    )
    if soften:
        prompt += f"\n\n{SOFTEN_NOTE}"
    model = get_model()
    response = await asyncio.to_thread(model.generate_content, prompt)
    return _extract_text(response)


async def generate_world_preview(category_label: str, category_hint: str, language_name: str) -> str:
    """A short, standalone world concept the player can accept or reroll —
    not part of the actual adventure yet, so no STATE tag or options here."""
    prompt = (
        f"Respond in: {language_name}.\n\n"
        "For THIS response only, ignore the closing STATE tag rule and the numbered-options "
        "rule — just answer with the requested text and nothing else.\n\n"
        f"Propose a short, evocative world concept (3-5 sentences) for a {category_label} "
        f"({category_hint}) adventure. Give it a distinct hook or twist so it doesn't feel generic."
    )
    model = get_model()
    response = await asyncio.to_thread(model.generate_content, prompt)
    return _extract_text(response)


async def generate_character_preview(
    gender: str,
    age: str,
    world_description: str | None,
    category_hint: str,
    language_name: str,
) -> str:
    """A short, standalone character concept the player can accept or
    reroll — not part of the actual adventure yet."""
    world_part = f"World: {world_description}" if world_description else f"World category hint: {category_hint}"
    prompt = (
        f"Respond in: {language_name}.\n\n"
        "For THIS response only, ignore the closing STATE tag rule and the numbered-options "
        "rule — just answer with the requested text and nothing else.\n\n"
        f"{world_part}\n\n"
        f"Propose a short character concept (3-5 sentences) fitting this world: gender — "
        f"{gender}, age — {age}. Include a name, class/profession, one notable trait, and a "
        "one-line backstory hook."
    )
    model = get_model()
    response = await asyncio.to_thread(model.generate_content, prompt)
    return _extract_text(response)


async def generate_new_adventure_opening(
    category_label: str,
    category_hint: str,
    world_description: str | None,
    character_brief: str,
    language_name: str,
    soften: bool = False,
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
        "THIS IS A NEW CHARACTER BEING CREATED — also invent, fitting the world and character: "
        "4-5 short physical/mental attributes (see rule 11, the ATTRS tag), some starting money "
        "in a currency that fits the world, and 2-4 starting inventory items. Reflect the money "
        f"and inventory in the closing STATE tag, and the attributes in the ATTRS tag placed "
        f"right before it. Start at full health: hp=max_hp={DEFAULT_HP} unless the character "
        "description implies a different max HP, in which case use that.\n\n"
        "Begin a new short adventure: describe the setting, the hook, and the opening scene "
        "with the character, then the list of action options."
    )
    if soften:
        prompt += f"\n\n{SOFTEN_NOTE}"
    model = get_model()
    response = await asyncio.to_thread(model.generate_content, prompt)
    return _extract_text(response)


async def generate_summary(existing_summary: str, recent_turns: list[str], language_name: str) -> str:
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
    response = await asyncio.to_thread(model.generate_content, prompt)
    return _extract_text(response)
