import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

import dice
import gemini_client
from game_state import GameState, delete_state, load_state, save_state

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


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


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Вітаю у текстовій DnD-пригоді!\n\n"
        "Команди:\n"
        "/new — почати нову пригоду (запитає ім'я персонажа)\n"
        "/roll — кинути кубик d20 вручну\n"
        "/reset — стерти прогрес і почати з нуля\n\n"
        "Після /new просто пиши свої дії текстом, або тисни кнопки з варіантами."
    )


@dp.message(Command("new"))
async def cmd_new(message: Message):
    user_id = message.from_user.id
    name = message.from_user.first_name or "Мандрівник"
    character = {"name": name, "class": "авантюрист", "hp": 20}

    await message.answer("Створюю пригоду для персонажа: " + name + "...")

    try:
        opening = gemini_client.generate_new_adventure_opening(character)
    except Exception as e:
        logger.exception("Gemini error")
        await message.answer(f"Помилка звернення до Gemini: {e}")
        return

    state = GameState(user_id=user_id, character=character)
    state.add_turn(f"[DM]: {opening}")
    save_state(state)

    options = extract_options(opening)
    kb = action_keyboard(options) if options else None
    await message.answer(opening, reply_markup=kb)


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    delete_state(message.from_user.id)
    await message.answer("Прогрес стерто. Напиши /new, щоб почати заново.")


@dp.message(Command("roll"))
async def cmd_roll(message: Message):
    result = dice.roll(20)
    await message.answer(f"🎲 {result.describe()}")


async def advance_story(message_or_callback, user_id: int, player_input: str):
    state = load_state(user_id)
    if state is None:
        await message_or_callback.answer("Спочатку почни пригоду командою /new")
        return

    state.add_turn(f"[Гравець]: {player_input}")

    try:
        story_text = gemini_client.generate_story_turn(
            summary=state.summary,
            recent_turns=state.recent_turns,
            character=state.character,
            player_input=player_input,
        )
    except Exception as e:
        logger.exception("Gemini error")
        await message_or_callback.answer(f"Помилка звернення до Gemini: {e}")
        return

    state.add_turn(f"[DM]: {story_text}")
    save_state(state)

    options = extract_options(story_text)
    kb = action_keyboard(options) if options else None
    await message_or_callback.answer(story_text, reply_markup=kb)


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_free_text(message: Message):
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
