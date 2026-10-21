import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deadbossbot")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎬 Улучшить качество"),
                KeyboardButton(text="💬 Добавить субтитры"),
            ],
            [
                KeyboardButton(text="⭐ Купить PRO"),
                KeyboardButton(text="👤 Профиль"),
            ],
        ],
        resize_keyboard=True,
    )


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я DeadBossBot.\n\n"
        "Отправь видео, и я помогу его улучшить или добавить субтитры.\n"
        "В этом проекте бот уже запущен и готов к работе."
        ,
        reply_markup=get_main_keyboard(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📘 Доступные команды:\n\n"
        "/start — старт\n"
        "/help — помощь\n"
        "/profile — профиль\n"
        "/cancel — отменить\n\n"
        "Или просто отправьте видео."
    )


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user = message.from_user
    await message.answer(
        f"👤 Профиль\n\n"
        f"ID: {user.id}\n"
        f"Username: @{user.username or '—'}\n"
        f"Имя: {user.first_name or '—'}"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    await message.answer("🛑 Текущая задача не запущена в этом минимальном режиме. Отправьте видео снова.")


@dp.message(F.text == "🎬 Улучшить качество")
async def btn_quality(message: Message):
    await message.answer("📹 Пришлите видео, и я обработаю его.")


@dp.message(F.text == "💬 Добавить субтитры")
async def btn_subtitles(message: Message):
    await message.answer("📹 Пришлите видео, и я добавлю субтитры.")


@dp.message(F.text == "⭐ Купить PRO")
async def btn_pro(message: Message):
    await message.answer("⭐ PRO ещё в разработке. Это базовый рабочий билд для деплоя.")


@dp.message(F.text == "👤 Профиль")
async def btn_profile(message: Message):
    await cmd_profile(message)


@dp.message(F.content_type.in_({"video", "document"}))
async def handle_media(message: Message):
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("Отправьте именно видеофайл.")
        return

    file = message.video or message.document
    if file is None:
        return

    await message.answer(
        "✅ Видео получено.\n"
        "Этот билд запускает бота и готов к дальнейшей доработке.\n"
        f"Название файла: {getattr(file, 'file_name', 'video.mp4')}"
    )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Add BOT_TOKEN to Render environment variables.")

    log.info("Starting DeadBossBot...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
