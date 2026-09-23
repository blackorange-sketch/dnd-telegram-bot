"""Thin wrapper around the Gemini API for generating DnD-style narration.

Uses the free-tier-friendly gemini-2.5-flash-lite model by default; swap
GEMINI_MODEL if you want richer prose from gemini-2.5-flash instead.
"""

import os

import google.generativeai as genai

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

SYSTEM_PROMPT = """\
Ти — досвідчений Майстер Підземель (DM), що веде інтерактивну текстову \
пригоду в стилі DnD українською мовою.

Правила твоєї відповіді:
1. Опиши коротко (3-6 речень) наслідки останньої дії гравця та поточну сцену.
2. Якщо гравцю потрібно було кинути кубик — тобі повідомлять результат кидка \
   окремо в повідомленні користувача; враховуй його у розповіді (успіх/провал/\
   критичний успіх на 20/критичний провал на 1).
3. Завжди завершуй відповідь списком 2-4 варіантів дій, пронумерованих \
   "1)", "2)" тощо. Один із варіантів час від часу може вимагати кидка кубика \
   (напиши прямо: "(потрібен кидок d20)").
4. Тримай тон пригодницьким, не надто похмурим, без жорстокого насильства \
   чи відверто заборонених тем.
5. Не вигадуй результат кубика сам — його завжди рахує гра.
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


def build_context(summary: str, recent_turns: list[str], character: dict) -> str:
    parts = []
    if character:
        parts.append(f"Персонаж гравця: {character}")
    if summary:
        parts.append(f"Стислий підсумок історії дотепер: {summary}")
    if recent_turns:
        parts.append("Останні репліки:\n" + "\n".join(recent_turns))
    return "\n\n".join(parts)


def generate_story_turn(summary: str, recent_turns: list[str], character: dict, player_input: str) -> str:
    context = build_context(summary, recent_turns, character)
    prompt = f"{context}\n\nДія гравця зараз: {player_input}"
    model = get_model()
    response = model.generate_content(prompt)
    return response.text.strip()


def generate_new_adventure_opening(
    category_label: str,
    category_hint: str,
    world_description: str | None,
    character_brief: str,
) -> str:
    """Generate the opening scene for a brand-new adventure.

    category_label/category_hint come from world_categories.py.
    world_description is either the player's own text or None (Gemini invents one).
    character_brief is a ready-made instruction describing the character —
    either the player's own description or gender/age for random generation.
    """
    if world_description:
        world_part = f"Опис світу від гравця: {world_description}"
    else:
        world_part = f"Вигадай сам світ, що пасує категорії ({category_hint})."

    prompt = (
        f"Категорія світу: {category_label} ({category_hint}).\n"
        f"{world_part}\n\n"
        f"{character_brief}\n\n"
        "Почни нову коротку пригоду: опиши місце дії, зав'язку та стартову сцену "
        "з персонажем, а тоді список варіантів дій."
    )
    model = get_model()
    response = model.generate_content(prompt)
    return response.text.strip()
