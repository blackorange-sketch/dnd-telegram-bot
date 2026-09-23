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
import world_categories as wc
from game_state import GameState, delete_state, load_state, save_state

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class Creation(StatesGroup):
    choosing_category = State()
    entering_world_description = State()
    choosing_character_method = State()
    entering_character_description = State()
    choosing_gender = State()
    entering_age = State()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Вітаю у текстовій DnD-пригоді!\n\n"
        "Команди:\n"
        "/new — почати нову пригоду (обрати світ і персонажа)\n"
        "/roll — кинути кубик d20 вручну\n"
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
# Step 1: /new -> choose world category
# ---------------------------------------------------------------------------

@dp.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Creation.choosing_category)
    await message.answer("Обери категорію світу для пригоди:", reply_markup=category_keyboard())


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
# Step 2: world description (custom text or random)
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
# Step 3: character — manual description OR random by gender/age
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

    if "character_description" in data:
        character_brief = (
            f"Гравець описав персонажа так: {data['character_description']}. "
            "Використай цей опис, за потреби доповни дрібними деталями (ім'я, клас, риси)."
        )
        character_record = {"description": data["character_description"]}
    else:
        gender = data.get("gender", "будь-яка")
        age = data.get("age", "будь-який")
        character_brief = (
            f"Придумай персонажа сам: стать — {gender}, вік — {age}. "
            "Вигадай ім'я, клас/професію та коротку передісторію, що пасує світу."
        )
        character_record = {"gender": gender, "age": age, "generated": True}

    await message.answer("Створюю світ і персонажа, зачекай кілька секунд...")

    try:
        opening = gemini_client.generate_new_adventure_opening(
            category_label=wc.label_for(category_key),
            category_hint=wc.description_for(category_key),
            world_description=world_description,
            character_brief=character_brief,
        )
    except Exception as e:
        logger.exception("Gemini error")
        await message.answer(f"Помилка звернення до Gemini: {e}")
        await state.clear()
        return

    character_record["category"] = category_key
    character_record["world_description"] = world_description

    game = GameState(user_id=message.from_user.id, character=character_record)
    game.add_turn(f"[DM]: {opening}")
    save_state(game)
    await state.clear()

    options = extract_options(opening)
    kb = action_keyboard(options) if options else None
    await message.answer(opening, reply_markup=kb)


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
        )
    except Exception as e:
        logger.exception("Gemini error")
        await message_or_callback.answer(f"Помилка звернення до Gemini: {e}")
        return

    game.add_turn(f"[DM]: {story_text}")
    save_state(game)

    options = extract_options(story_text)
    kb = action_keyboard(options) if options else None
    await message_or_callback.answer(story_text, reply_markup=kb)


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
    result = dice.roll(sides)
    await callback.answer(f"Випало: {result.value}")
    await advance_story(
        callback.message,
        callback.from_user.id,
        f"Я кинув кубик d{sides} і випало {result.value}.",
    )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (check your .env file)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
