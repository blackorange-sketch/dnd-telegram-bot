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
import world_categories as wc
from game_state import GameState, delete_state, load_state, save_state

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


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


def category_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key in wc.all_keys():
        rows.append([InlineKeyboardButton(text=wc.label_for(key), callback_data=f"cat:{key}")])
    rows.append([InlineKeyboardButton(text=wc.RANDOM_LABEL, callback_data=f"cat:{wc.RANDOM_KEY}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def world_description_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Хай світ вигадає AI", callback_data="world:random")],
    ])


def character_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Опишу персонажа сам", callback_data="charmethod:manual")],
        [InlineKeyboardButton(text="🎲 Згенерувати випадково", callback_data="charmethod:random")],
    ])


def gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Чоловіча", callback_data="gender:чоловіча"),
            InlineKeyboardButton(text="Жіноча", callback_data="gender:жіноча"),
            InlineKeyboardButton(text="Будь-яка", callback_data="gender:будь-яка"),
        ]
    ])


def action_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    """Build inline buttons from a numbered list like '1) Do the thing'."""
    buttons = []
    for i, _ in enumerate(options, start=1):
        buttons.append(InlineKeyboardButton(text=str(i), callback_data=f"choice:{i}"))
    buttons.append(InlineKeyboardButton(text="🎲 d20", callback_data="roll:20"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def extract_options(story_text: str) -> list[str]:
    """Pull numbered options ('1) ...', '2) ...') out of the model's reply."""
    return re.findall(r"^\s*\d\)\s*.+$", story_text, flags=re.MULTILINE)


def hp_status_line(hp: int, max_hp: int) -> str:
    return f"❤️ HP: {hp}/{max_hp}"


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Вітаю у текстовій DnD-пригоді!\n\n"
        "Команди:\n"
        "/new — почати нову пригоду (мова, світ, персонаж)\n"
        "/roll — кинути кубик d20 вручну (тестовий, без прив'язки до гри)\n"
        "/reset — стерти прогрес і почати з нуля\n\n"
        "Після створення пригоди просто пиши дії текстом або тисни кнопки."
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
    delete_state(message.from_user.id)
    await state.clear()
    await message.answer("Прогрес стерто. Напиши /new, щоб почати заново.")


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
    await state.set_state(Creation.choosing_language)
    await message.answer("Обери мову пригоди:", reply_markup=language_keyboard())


@dp.callback_query(Creation.choosing_language, F.data.startswith("lang:"))
async def process_language(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    await state.update_data(language_key=key, language_name=lang.prompt_name_for(key))
    await callback.answer(lang.label_for(key))
    await callback.message.answer("Обери категорію світу для пригоди:", reply_markup=category_keyboard())
    await state.set_state(Creation.choosing_category)


# ---------------------------------------------------------------------------
# Step 2: choose world category
# ---------------------------------------------------------------------------

@dp.callback_query(Creation.choosing_category, F.data.startswith("cat:"))
async def process_category(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key == wc.RANDOM_KEY:
        key = random.choice(wc.all_keys())
    await state.update_data(category_key=key)
    await callback.answer(f"Обрано: {wc.label_for(key)}")
    await callback.message.answer(
        "Опиши своїми словами світ, де відбуватиметься історія (кілька речень),\n"
        "або натисни кнопку, щоб я вигадав світ сам:",
        reply_markup=world_description_keyboard(),
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
    await state.update_data(world_description=None)
    await callback.answer("Гаразд, вигадаю світ сам")
    await ask_character_method(callback.message, state)


async def ask_character_method(message: Message, state: FSMContext):
    await message.answer("Як створимо персонажа?", reply_markup=character_method_keyboard())
    await state.set_state(Creation.choosing_character_method)


# ---------------------------------------------------------------------------
# Step 4: character — manual description OR random by gender/age
# ---------------------------------------------------------------------------

@dp.callback_query(Creation.choosing_character_method, F.data.startswith("charmethod:"))
async def process_character_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split(":", 1)[1]
    await callback.answer()
    if method == "manual":
        await callback.message.answer(
            "Опиши персонажа: ім'я, стать, вік, клас/професія, коротка передісторія — "
            "що вважаєш за потрібне:"
        )
        await state.set_state(Creation.entering_character_description)
    else:
        await callback.message.answer("Обери стать персонажа:", reply_markup=gender_keyboard())
        await state.set_state(Creation.choosing_gender)


@dp.message(Creation.entering_character_description)
async def process_character_description(message: Message, state: FSMContext):
    await state.update_data(character_description=message.text)
    await finalize_creation(message, state)


@dp.callback_query(Creation.choosing_gender, F.data.startswith("gender:"))
async def process_gender(callback: CallbackQuery, state: FSMContext):
    gender = callback.data.split(":", 1)[1]
    await state.update_data(gender=gender)
    await callback.answer()
    await callback.message.answer(
        "Вкажи вік персонажа числом (наприклад, 27), або напиши 'будь-який':"
    )
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
    category_key = data.get("category_key", random.choice(wc.all_keys()))
    world_description = data.get("world_description")
    language_name = data.get("language_name", lang.prompt_name_for(lang.DEFAULT_LANGUAGE))

    if "character_description" in data:
        character_brief = (
            f"The player described the character like this: {data['character_description']}. "
            "Use this description, filling in small extra details if needed (name, class, traits)."
        )
        character_record = {"description": data["character_description"]}
    else:
        gender = data.get("gender", "будь-яка")
        age = data.get("age", "будь-який")
        character_brief = (
            f"Invent the character yourself: gender — {gender}, age — {age}. "
            "Make up a name, class/profession, and a short backstory that fits the world."
        )
        character_record = {"gender": gender, "age": age, "generated": True}

    await message.answer("Створюю світ і персонажа, зачекай кілька секунд...")

    try:
        opening = gemini_client.generate_new_adventure_opening(
            category_label=wc.label_for(category_key),
            category_hint=wc.description_for(category_key),
            world_description=world_description,
            character_brief=character_brief,
            language_name=language_name,
        )
    except Exception as e:
        logger.exception("Gemini error")
        await message.answer(f"Помилка звернення до Gemini: {e}")
        await state.clear()
        return

    clean_text, hp, max_hp = gemini_client.parse_hp_tag(opening)
    hp = hp if hp is not None else gemini_client.DEFAULT_HP
    max_hp = max_hp if max_hp is not None else gemini_client.DEFAULT_HP

    character_record["category"] = category_key
    character_record["world_description"] = world_description
    character_record["hp"] = hp
    character_record["max_hp"] = max_hp

    game = GameState(user_id=message.from_user.id, character=character_record, language=language_name)
    game.add_turn(f"[DM]: {clean_text}")
    save_state(game)
    await state.clear()

    options = extract_options(clean_text)
    kb = action_keyboard(options) if options else None
    await message.answer(f"{clean_text}\n\n{hp_status_line(hp, max_hp)}", reply_markup=kb)


# ---------------------------------------------------------------------------
# Ongoing story turns (once a game is active and no creation flow is running)
# ---------------------------------------------------------------------------

async def advance_story(message_or_callback, user_id: int, player_input: str):
    game = load_state(user_id)
    if game is None:
        await message_or_callback.answer("Спочатку почни пригоду командою /new")
        return

    game.add_turn(f"[Гравець]: {player_input}")

    try:
        story_text = gemini_client.generate_story_turn(
            summary=game.summary,
            recent_turns=game.recent_turns,
            character=game.character,
            player_input=player_input,
            language_name=game.language,
        )
    except Exception as e:
        logger.exception("Gemini error")
        await message_or_callback.answer(f"Помилка звернення до Gemini: {e}")
        return

    clean_text, hp, max_hp = gemini_client.parse_hp_tag(story_text)
    if hp is not None:
        game.character["hp"] = hp
        game.character["max_hp"] = max_hp if max_hp is not None else game.character.get("max_hp", hp)

    game.add_turn(f"[DM]: {clean_text}")
    save_state(game)

    options = extract_options(clean_text)
    kb = action_keyboard(options) if options else None

    reply_text = clean_text
    if hp is not None:
        reply_text += f"\n\n{hp_status_line(hp, game.character.get('max_hp', hp))}"
        if hp <= 0:
            reply_text += "\n\n💀 Персонаж втратив свідомість/загинув. Напиши /new, щоб почати заново."

    await message_or_callback.answer(reply_text, reply_markup=kb)


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_free_text(message: Message, state: FSMContext):
    # If we're in the middle of the creation wizard, the dedicated handlers
    # above already caught it; this only fires once no Creation state is set.
    current_state = await state.get_state()
    if current_state is not None:
        return
    await advance_story(message, message.from_user.id, message.text)


@dp.callback_query(F.data.startswith("choice:"))
async def handle_choice(callback: CallbackQuery):
    choice_num = callback.data.split(":")[1]
    await callback.answer()
    await advance_story(callback.message, callback.from_user.id, f"Обираю варіант {choice_num}")


@dp.callback_query(F.data.startswith("roll:"))
async def handle_roll_button(callback: CallbackQuery):
    sides = int(callback.data.split(":")[1])
    game = load_state(callback.from_user.id)
    hp = game.character.get("hp", gemini_client.DEFAULT_HP) if game else gemini_client.DEFAULT_HP
    max_hp = game.character.get("max_hp", gemini_client.DEFAULT_HP) if game else gemini_client.DEFAULT_HP
    penalty = dice.hp_penalty(hp, max_hp)

    result = dice.roll(sides, modifier=penalty)
    await callback.answer(f"Випало: {result.value}" + (f" (штраф {penalty} через поранення)" if penalty else ""))

    if penalty:
        description = (
            f"Кидаю кубик d{sides}: базове значення {result.value}, "
            f"штраф {penalty} через поранення, підсумок {result.total}."
        )
    else:
        description = f"Кидаю кубик d{sides}: випало {result.value}."

    await advance_story(callback.message, callback.from_user.id, description)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (check your .env file)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
