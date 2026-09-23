import asyncio
import logging
import os
import random
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

import dice
import gemini_client
import languages as lang
import ui_strings as ui
import world_categories as wc
from game_state import SUMMARY_EVERY_N_TURNS, GameState, delete_state, load_state, save_state

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Simple per-user lock so a second button press while a Gemini call is still
# in flight doesn't kick off a second, overlapping story continuation.
active_users: set[int] = set()

TIER_EMOJI = {
    "critical_failure": "💥",
    "failure": "❌",
    "partial_success": "➖",
    "success": "✅",
    "critical_success": "🌟",
}

OPTION_LINE_RE = re.compile(r"^\s*\d\)\s*.+$", re.MULTILINE)


class Creation(StatesGroup):
    choosing_language = State()
    choosing_category = State()
    entering_world_description = State()
    choosing_character_method = State()
    entering_character_description = State()
    choosing_gender = State()
    entering_age = State()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def language_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=lang.label_for(key), callback_data=f"lang:{key}")]
            for key in lang.all_keys()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_keyboard(lang_key: str) -> InlineKeyboardMarkup:
    rows = []
    for key in wc.all_keys():
        rows.append([InlineKeyboardButton(text=wc.label_for(key, lang_key), callback_data=f"cat:{key}")])
    rows.append([InlineKeyboardButton(text=wc.random_label(lang_key), callback_data=f"cat:{wc.RANDOM_KEY}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def world_description_keyboard(lang_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ui.t(lang_key, "world_random_button"), callback_data="world:random")],
    ])


def character_method_keyboard(lang_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ui.t(lang_key, "character_manual_button"), callback_data="charmethod:manual")],
        [InlineKeyboardButton(text=ui.t(lang_key, "character_random_button"), callback_data="charmethod:random")],
    ])


def gender_keyboard(lang_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=ui.t(lang_key, "gender_male"), callback_data="gender:male"),
            InlineKeyboardButton(text=ui.t(lang_key, "gender_female"), callback_data="gender:female"),
            InlineKeyboardButton(text=ui.t(lang_key, "gender_any"), callback_data="gender:any"),
        ]
    ])


def action_keyboard(option_count: int) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=str(i), callback_data=f"choice:{i}") for i in range(1, option_count + 1)]
    buttons.append(InlineKeyboardButton(text="🎲 d20", callback_data="roll:20"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def format_options(story_text: str, lang_key: str) -> tuple[str, list[dict]]:
    """Find numbered option lines, strip the internal [ROLL] marker and
    replace it with a localized hint for display, and return the cleaned
    text plus a list of {"text": ..., "requires_roll": bool} in order."""
    options: list[dict] = []

    def repl(match: re.Match) -> str:
        line = match.group(0)
        requires_roll = gemini_client.ROLL_MARKER in line
        clean_line = line.replace(gemini_client.ROLL_MARKER, "").rstrip()
        # Strip the leading "N) " to store just the option's own text.
        text_only = re.sub(r"^\s*\d\)\s*", "", clean_line)
        options.append({"text": text_only, "requires_roll": requires_roll})
        if requires_roll:
            clean_line += " " + ui.t(lang_key, "requires_roll_hint")
        return clean_line

    new_text = OPTION_LINE_RE.sub(repl, story_text)
    return new_text, options


async def clear_keyboard(callback: CallbackQuery):
    """Remove buttons from the message that was just acted on, so stale
    buttons from an earlier turn can't be pressed again later."""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass  # message may already have no markup, or be too old to edit


def status_line(lang_key: str, character: dict) -> str:
    parts = [f"{ui.t(lang_key, 'hp_label')}: {character.get('hp')}/{character.get('max_hp')}"]
    if character.get("money"):
        parts.append(f"💰 {character['money']}")
    return " | ".join(parts)


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
        game.recent_turns = game.recent_turns[-2:]  # keep just a little recent continuity
    except Exception:
        logger.exception("Summary generation failed, skipping this round")


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    game = load_state(message.from_user.id)
    lang_key = game.language if game else lang.DEFAULT_LANGUAGE
    await message.answer(ui.t(lang_key, "start"))


@dp.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
    game = load_state(message.from_user.id)
    lang_key = game.language if game else lang.DEFAULT_LANGUAGE
    delete_state(message.from_user.id)
    await state.clear()
    active_users.discard(message.from_user.id)
    await message.answer(ui.t(lang_key, "reset_done"))


@dp.message(Command("roll"))
async def cmd_roll(message: Message):
    result = dice.roll(20)
    await message.answer(f"🎲 {result.describe()}")


# ---------------------------------------------------------------------------
# Step 1: /new -> choose language
# ---------------------------------------------------------------------------

@dp.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await state.clear()
    active_users.discard(message.from_user.id)
    await state.set_state(Creation.choosing_language)
    await message.answer(
        "🌐 Choose language / Обери мову / Выбери язык / Wybierz język:",
        reply_markup=language_keyboard(),
    )


@dp.callback_query(Creation.choosing_language, F.data.startswith("lang:"))
async def process_language(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    await state.update_data(language_key=key)
    await callback.answer(lang.label_for(key))
    await clear_keyboard(callback)
    await callback.message.answer(ui.t(key, "choose_category"), reply_markup=category_keyboard(key))
    await state.set_state(Creation.choosing_category)


# ---------------------------------------------------------------------------
# Step 2: choose world category
# ---------------------------------------------------------------------------

@dp.callback_query(Creation.choosing_category, F.data.startswith("cat:"))
async def process_category(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang_key = data.get("language_key", lang.DEFAULT_LANGUAGE)

    key = callback.data.split(":", 1)[1]
    if key == wc.RANDOM_KEY:
        key = random.choice(wc.all_keys())
    await state.update_data(category_key=key)
    await callback.answer(wc.label_for(key, lang_key))
    await clear_keyboard(callback)
    await callback.message.answer(
        ui.t(lang_key, "describe_world"),
        reply_markup=world_description_keyboard(lang_key),
    )
    await state.set_state(Creation.entering_world_description)


# ---------------------------------------------------------------------------
# Step 3: world description (custom text or random)
# ---------------------------------------------------------------------------

@dp.message(Creation.entering_world_description)
async def process_world_description_text(message: Message, state: FSMContext):
    await state.update_data(world_description=message.text)
    await ask_character_method(message, state)


@dp.callback_query(Creation.entering_world_description, F.data == "world:random")
async def process_world_description_random(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang_key = data.get("language_key", lang.DEFAULT_LANGUAGE)
    await state.update_data(world_description=None)
    await callback.answer(ui.t(lang_key, "world_random_ack"))
    await clear_keyboard(callback)
    await ask_character_method(callback.message, state)


async def ask_character_method(message: Message, state: FSMContext):
    data = await state.get_data()
    lang_key = data.get("language_key", lang.DEFAULT_LANGUAGE)
    await message.answer(ui.t(lang_key, "character_method_prompt"), reply_markup=character_method_keyboard(lang_key))
    await state.set_state(Creation.choosing_character_method)


# ---------------------------------------------------------------------------
# Step 4: character — manual description OR random by gender/age
# ---------------------------------------------------------------------------

@dp.callback_query(Creation.choosing_character_method, F.data.startswith("charmethod:"))
async def process_character_method(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang_key = data.get("language_key", lang.DEFAULT_LANGUAGE)
    method = callback.data.split(":", 1)[1]
    await callback.answer()
    await clear_keyboard(callback)
    if method == "manual":
        await callback.message.answer(ui.t(lang_key, "describe_character_prompt"))
        await state.set_state(Creation.entering_character_description)
    else:
        await callback.message.answer(ui.t(lang_key, "choose_gender"), reply_markup=gender_keyboard(lang_key))
        await state.set_state(Creation.choosing_gender)


@dp.message(Creation.entering_character_description)
async def process_character_description(message: Message, state: FSMContext):
    await state.update_data(character_description=message.text)
    await finalize_creation(message, state)


@dp.callback_query(Creation.choosing_gender, F.data.startswith("gender:"))
async def process_gender(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang_key = data.get("language_key", lang.DEFAULT_LANGUAGE)
    gender = callback.data.split(":", 1)[1]  # male / female / any
    await state.update_data(gender=gender)
    await callback.answer()
    await clear_keyboard(callback)
    await callback.message.answer(ui.t(lang_key, "enter_age_prompt"))
    await state.set_state(Creation.entering_age)


@dp.message(Creation.entering_age)
async def process_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    await finalize_creation(message, state)


# ---------------------------------------------------------------------------
# Final step: call Gemini and start the adventure
# ---------------------------------------------------------------------------

async def finalize_creation(message: Message, state: FSMContext):
    data = await state.get_data()
    lang_key = data.get("language_key", lang.DEFAULT_LANGUAGE)
    language_name = lang.prompt_name_for(lang_key)
    category_key = data.get("category_key", random.choice(wc.all_keys()))
    world_description = data.get("world_description")

    if "character_description" in data:
        character_brief = (
            f"The player described the character like this: {data['character_description']}. "
            "Use this description, filling in small extra details if needed (name, class, traits)."
        )
        character_record = {"description": data["character_description"]}
    else:
        gender = data.get("gender", "any")
        age = data.get("age", "any")
        character_brief = (
            f"Invent the character yourself: gender — {gender}, age — {age}. "
            "Make up a name, class/profession, and a short backstory that fits the world."
        )
        character_record = {"gender": gender, "age": age, "generated": True}

    await message.answer(ui.t(lang_key, "creating"))

    try:
        opening = gemini_client.generate_new_adventure_opening(
            category_label=wc.label_for(category_key, lang_key),
            category_hint=wc.hint_for(category_key),
            world_description=world_description,
            character_brief=character_brief,
            language_name=language_name,
        )
    except Exception as e:
        logger.exception("Gemini error")
        await message.answer(ui.t(lang_key, "gemini_error", error=e))
        await state.clear()
        return

    clean_text, parsed_state = gemini_client.parse_state_tag(opening)
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

    display_text, options = format_options(clean_text, lang_key)

    game = GameState(user_id=message.from_user.id, character=character_record, language=lang_key)
    game.add_turn(f"[DM]: {clean_text}")
    game.pending_options = options
    save_state(game)
    await state.clear()

    kb = action_keyboard(len(options)) if options else None
    await message.answer(f"{display_text}\n\n{status_line(lang_key, character_record)}", reply_markup=kb)


# ---------------------------------------------------------------------------
# Ongoing story turns (once a game is active and no creation flow is running)
# ---------------------------------------------------------------------------

async def advance_story(message_or_callback, user_id: int, player_input: str):
    if user_id in active_users:
        game = load_state(user_id)
        lang_key = game.language if game else lang.DEFAULT_LANGUAGE
        await message_or_callback.answer(ui.t(lang_key, "please_wait"))
        return

    game = load_state(user_id)
    if game is None:
        await message_or_callback.answer(ui.t(lang.DEFAULT_LANGUAGE, "no_active_game"))
        return

    lang_key = game.language
    active_users.add(user_id)
    try:
        game.add_turn(f"[Player]: {player_input}")

        try:
            story_text = gemini_client.generate_story_turn(
                summary=game.summary,
                recent_turns=game.recent_turns,
                character=game.character,
                player_input=player_input,
                language_name=lang.prompt_name_for(lang_key),
            )
        except Exception as e:
            logger.exception("Gemini error")
            await message_or_callback.answer(ui.t(lang_key, "gemini_error", error=e))
            return

        clean_text, parsed_state = gemini_client.parse_state_tag(story_text)
        if "hp" in parsed_state:
            game.character["hp"] = parsed_state["hp"]
            game.character["max_hp"] = parsed_state.get("max_hp", game.character.get("max_hp"))
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

        kb = action_keyboard(len(options)) if options else None

        reply_text = f"{display_text}\n\n{status_line(lang_key, game.character)}"
        hp = game.character.get("hp")
        if hp is not None and hp <= 0:
            reply_text += f"\n\n{ui.t(lang_key, 'defeated')}"

        await message_or_callback.answer(reply_text, reply_markup=kb)
    finally:
        active_users.discard(user_id)


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_free_text(message: Message, state: FSMContext):
    # If we're in the middle of the creation wizard, the dedicated handlers
    # above already caught it; this only fires once no Creation state is set.
    current_state = await state.get_state()
    if current_state is not None:
        return
    await advance_story(message, message.from_user.id, message.text)


async def do_roll_and_describe(callback: CallbackQuery, lang_key: str, game: GameState, sides: int = 20) -> str:
    """Roll the die, post a standalone result message with the outcome
    tier, and return the action text to feed into the story generation."""
    hp = game.character.get("hp", gemini_client.DEFAULT_HP)
    max_hp = game.character.get("max_hp", gemini_client.DEFAULT_HP)
    penalty = dice.hp_penalty(hp, max_hp)

    result = dice.roll(sides, modifier=penalty)
    tier = dice.classify(result.value, result.total)
    penalty_note = ui.t(lang_key, "dice_penalty_note", penalty=penalty) if penalty else ""

    roll_message = (
        f"{ui.t(lang_key, 'dice_roll_label', sides=sides)}: {result.value}{penalty_note}\n"
        f"{ui.t(lang_key, 'dice_total', total=result.total)}\n"
        f"{TIER_EMOJI[tier]} {ui.t(lang_key, f'tier_{tier}')}"
    )
    await callback.message.answer(roll_message)

    return f"roll result: {tier} (natural {result.value}, total {result.total})"


@dp.callback_query(F.data.startswith("choice:"))
async def handle_choice(callback: CallbackQuery):
    game = load_state(callback.from_user.id)
    lang_key = game.language if game else lang.DEFAULT_LANGUAGE

    if callback.from_user.id in active_users:
        await callback.answer(ui.t(lang_key, "please_wait"))
        return

    choice_num = int(callback.data.split(":")[1])
    await callback.answer()
    await clear_keyboard(callback)

    option = None
    if game and game.pending_options and 1 <= choice_num <= len(game.pending_options):
        option = game.pending_options[choice_num - 1]

    if option is None:
        action_text = ui.t(lang_key, "choosing_option", n=choice_num)
    elif option.get("requires_roll"):
        roll_summary = await do_roll_and_describe(callback, lang_key, game)
        action_text = f"{option['text']} — {roll_summary}"
    else:
        action_text = option["text"]

    await advance_story(callback.message, callback.from_user.id, action_text)


@dp.callback_query(F.data.startswith("roll:"))
async def handle_roll_button(callback: CallbackQuery):
    game = load_state(callback.from_user.id)
    lang_key = game.language if game else lang.DEFAULT_LANGUAGE

    if callback.from_user.id in active_users:
        await callback.answer(ui.t(lang_key, "please_wait"))
        return

    sides = int(callback.data.split(":")[1])
    await callback.answer()
    await clear_keyboard(callback)

    roll_summary = await do_roll_and_describe(callback, lang_key, game, sides=sides)
    await advance_story(callback.message, callback.from_user.id, f"I roll a die — {roll_summary}")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (check your .env file)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
