import asyncio
import logging
import os
import sqlite3
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from flask import Flask, jsonify

BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a number. Example: ADMIN_ID=123456789") from exc

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "deadboss.db"
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
PORT = int(os.getenv("PORT", "10000"))
ALERT_COOLDOWN_SECONDS = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deadbossbot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add BOT_TOKEN to Render Environment Variables.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
app = Flask(__name__)

last_admin_alerts = {}
admin_alert_lock = Lock()


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def ensure_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT,
                pro_until INTEGER DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                last_task_at TEXT,
                total_tasks INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                task_type TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                error TEXT,
                output_name TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('processing_enabled', '1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('queue_paused', '0')"
        )
        conn.commit()


def ensure_user(user_id: int, username: str | None, first_name: str | None):
    with db_connect() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at, pro_until, is_blocked, last_task_at, total_tasks) VALUES (?, ?, ?, ?, 0, 0, ?, 0)",
                (
                    user_id,
                    username or "",
                    first_name or "",
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
        else:
            conn.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username or "", first_name or "", user_id),
            )
            conn.commit()


def get_setting(key: str, default: str = "0") -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def is_pro_active(user_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT pro_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return False
    return int(row["pro_until"]) > int(time.time())


def set_pro_for_user(user_id: int, days: int = 30, user_name: str | None = None):
    ensure_user(user_id, user_name, user_name)
    now = int(time.time())
    with db_connect() as conn:
        row = conn.execute("SELECT pro_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
        current = int(row["pro_until"]) if row and row["pro_until"] else now
        new_expiry = max(current, now) + int(timedelta(days=days).total_seconds())
        conn.execute("UPDATE users SET pro_until = ? WHERE user_id = ?", (new_expiry, user_id))
        conn.commit()


def remove_pro_for_user(user_id: int):
    with db_connect() as conn:
        conn.execute("UPDATE users SET pro_until = 0 WHERE user_id = ?", (user_id,))
        conn.commit()


def get_user_by_username_or_id(identifier: str):
    with db_connect() as conn:
        if str(identifier).startswith("@"): 
            identifier = identifier[1:]
        user = conn.execute(
            "SELECT user_id, username, first_name, pro_until, is_blocked FROM users WHERE user_id = ? OR username = ? LIMIT 1",
            (identifier, identifier),
        ).fetchone()
    return dict(user) if user else None


def get_all_users(limit: int = 20):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id, username, first_name, pro_until, is_blocked FROM users ORDER BY user_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def send_admin_alert(category: str, message: str):
    if ADMIN_ID == 0:
        return

    now = time.time()
    with admin_alert_lock:
        last_time = last_admin_alerts.get(category, 0)
        if now - last_time < ALERT_COOLDOWN_SECONDS:
            return
        last_admin_alerts[category] = now

    text = (
        "🚨 DeadBossBot alert\n\n"
        f"Time: {utc_now_iso()}\n"
        f"Category: {category}\n\n"
        f"{message}"
    )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(bot.send_message(chat_id=ADMIN_ID, text=text[:4000]))
    except RuntimeError:
        try:
            asyncio.run(bot.send_message(chat_id=ADMIN_ID, text=text[:4000]))
        except Exception as send_exc:
            log.exception("Failed to send admin alert: %s", send_exc)
    except Exception as send_exc:
        log.exception("Failed to schedule admin alert: %s", send_exc)


@app.get("/health")
def health_check():
    return jsonify({"status": "ok", "service": "deadbossbot"}), 200


@app.get("/")
def root():
    return jsonify({"status": "ok", "service": "deadbossbot"}), 200


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
            [
                KeyboardButton(text="🛠 Админ"),
            ],
        ],
        resize_keyboard=True,
    )


def get_private_welcome_text():
    return (
        "Привет! Я DeadBossBot.\n\n"
        "Я умею:\n"
        "• улучшать качество видео\n"
        "• добавлять субтитры\n"
        "• держать тебя в курсе статуса обработки\n\n"
        "Нажми /start, выбери действие в меню или просто отправь видео."
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
                InlineKeyboardButton(text="🔄 Очередь", callback_data="admin:toggle_processing"),
            ],
            [
                InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:list_users"),
                InlineKeyboardButton(text="🧹 Очистить ошибки", callback_data="admin:clear_errors"),
            ],
            [
                InlineKeyboardButton(text="✅ Выдать PRO", callback_data="admin:grant_pro"),
                InlineKeyboardButton(text="❌ Снять PRO", callback_data="admin:revoke_pro"),
            ],
        ]
    )


@dp.message(Command("start"))
async def cmd_start(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(get_private_welcome_text(), reply_markup=get_main_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📘 Доступные команды:\n\n"
        "/start — старт\n"
        "/help — помощь\n"
        "/profile — профиль\n"
        "/admin — админ-панель\n"
        "/grantpro <id|@user> 30 — выдать PRO\n"
        "/revokepro <id|@user> — снять PRO\n"
        "/cancel — отменить\n\n"
        "Или просто отправьте видео."
    )


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT username, first_name, pro_until, total_tasks FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    pro_text = "✅ PRO активен" if is_pro_active(user_id) else "❌ PRO не активен"
    if row and row["pro_until"]:
        expiry = datetime.fromtimestamp(int(row["pro_until"]), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        pro_text = f"✅ PRO активен до {expiry}"
    total_tasks = int(row["total_tasks"]) if row else 0
    await message.answer(
        f"👤 Профиль\n\n"
        f"ID: {user_id}\n"
        f"Username: @{message.from_user.username or '—'}\n"
        f"{pro_text}\n"
        f"Всего задач: {total_tasks}"
    )


@dp.message(Command("buy_pro"))
async def cmd_buy_pro(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if is_pro_active(message.from_user.id):
        await message.answer("✅ У тебя уже активен PRO.")
        return
    await message.answer(
        "⭐ PRO доступен через админ-панель.\n\n"
        "Если нужно — администратор может выдать доступ вручную.\n"
        "Для владельца бота доступна команда /grantpro <id|@username> 30"
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
        return
    await admin_panel(message)


@dp.message(Command("grantpro"))
async def cmd_grantpro(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Формат: /grantpro <user_id|@username> <days>\nПример: /grantpro 123456789 30")
        return

    target = parts[1]
    try:
        days = int(parts[2])
    except ValueError:
        await message.answer("Дни должны быть числом, например: 30")
        return

    found = get_user_by_username_or_id(target)
    if found is None:
        try:
            target_id = int(target)
            ensure_user(target_id, None, None)
            found = {"user_id": target_id, "username": "", "first_name": ""}
        except ValueError:
            await message.answer(f"Пользователь {target} не найден и не распознан как ID.")
            return

    set_pro_for_user(int(found["user_id"]), days=days, user_name=(found.get("username") or found.get("first_name")))
    await message.answer(f"✅ PRO выдан пользователю {found['user_id']} на {days} дней.")
    try:
        await bot.send_message(chat_id=int(found["user_id"]), text=f"✅ Администратор выдал тебе PRO на {days} дней.")
    except Exception:
        pass


@dp.message(Command("revokepro"))
async def cmd_revokepro(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /revokepro <user_id|@username>")
        return

    target = parts[1]
    found = get_user_by_username_or_id(target)
    if found is None:
        try:
            target_id = int(target)
            ensure_user(target_id, None, None)
            found = {"user_id": target_id}
        except ValueError:
            await message.answer(f"Пользователь {target} не найден.")
            return

    remove_pro_for_user(int(found["user_id"]))
    await message.answer(f"✅ PRO снят у пользователя {found['user_id']}.")


async def admin_panel(message: Message):
    with db_connect() as conn:
        users_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        pro_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE pro_until > ?", (int(time.time()),)).fetchone()["c"]
        processing_enabled = get_setting("processing_enabled", "1")
        task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]

    text = (
        "🛠 Админ-панель\n\n"
        f"Пользователей: {users_count}\n"
        f"PRO: {pro_count}\n"
        f"Задач: {task_count}\n"
        f"Обработка: {'включена' if processing_enabled == '1' else 'отключена'}"
    )
    await message.answer(text, reply_markup=admin_keyboard())


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("🚫 Доступ запрещён.")
        return

    action = callback.data
    if action == "admin:stats":
        await admin_panel(callback.message) if callback.message else None
    elif action == "admin:toggle_processing":
        current = get_setting("processing_enabled", "1")
        new = "0" if current == "1" else "1"
        set_setting("processing_enabled", new)
        await callback.message.answer(f"🧠 Обработка {'отключена' if new == '0' else 'включена'}.")
    elif action == "admin:clear_errors":
        with db_connect() as conn:
            conn.execute("DELETE FROM tasks WHERE status = 'error'")
            conn.commit()
        await callback.message.answer("✅ Ошибки очищены.")
    elif action == "admin:list_users":
        rows = get_all_users(10)
        if not rows:
            await callback.message.answer("Пользователи ещё не зарегистрировались.")
            return
        lines = []
        for row in rows:
            pro_status = "PRO" if int(row["pro_until"]) > int(time.time()) else "—"
            lines.append(f"{row['user_id']} | @{row['username'] or '—'} | {pro_status}")
        await callback.message.answer("👥 Последние пользователи:\n\n" + "\n".join(lines[:10]))
    elif action == "admin:grant_pro":
        await callback.message.answer("✅ Выдать PRO можно командой:\n`/grantpro <id|@username> 30`", parse_mode="Markdown")
    elif action == "admin:revoke_pro":
        await callback.message.answer("✅ Снять PRO можно командой:\n`/revokepro <id|@username>`", parse_mode="Markdown")

    await callback.answer()


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
    await cmd_buy_pro(message)


@dp.message(F.text == "👤 Профиль")
async def btn_profile(message: Message):
    await cmd_profile(message)


@dp.message(F.text == "🛠 Админ")
async def btn_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
        return
    await admin_panel(message)


@dp.message(F.chat.type == "private", F.text == "")
async def empty_private_message(message: Message):
    await message.answer(get_private_welcome_text(), reply_markup=get_main_keyboard())


@dp.message(F.chat.type == "private")
async def private_fallback(message: Message):
    if message.text is None or message.text.strip() == "":
        await message.answer(get_private_welcome_text(), reply_markup=get_main_keyboard())
        return

    if message.text.strip().lower() in {"hello", "hi", "привет", "ку", "start"}:
        await message.answer(get_private_welcome_text(), reply_markup=get_main_keyboard())
        return

    # Don't spam the welcome text after every user message. Only show when text is clearly a greeting.
    if message.text.strip().lower() in {"help", "/help"}:
        await cmd_help(message)
        return

    if message.text.strip().lower() in {"profile", "/profile"}:
        await cmd_profile(message)
        return

    await message.answer(
        "Я готов помочь с видео.\n\n"
        "Выбери нужный пункт в меню или отправь видео напрямую.",
        reply_markup=get_main_keyboard(),
    )


@dp.message(F.content_type.in_({"video", "document"}))
async def handle_media(message: Message):
    try:
        if message.document and not (message.document.mime_type or "").startswith("video/"):
            await message.answer("Отправьте именно видеофайл.")
            return

        file = message.video or message.document
        if file is None:
            return

        ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        await message.answer(
            "✅ Видео получено.\n"
            "Я готов обработать его дальше.\n"
            f"Название файла: {getattr(file, 'file_name', 'video.mp4')}"
        )
    except Exception as exc:
        log.exception("Error while handling media message")
        send_admin_alert("media_error", f"User: {message.from_user.id}\nReason: {type(exc).__name__}: {exc}\n\nTraceback:\n{traceback.format_exc()[:2000]}")
        await message.answer("❌ Произошла ошибка при обработке видео. Администратор уже уведомлён.")


async def bot_main():
    try:
        ensure_db()
        log.info("Starting DeadBossBot polling...")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as exc:
        log.exception("Bot polling crashed")
        send_admin_alert("bot_crash", f"Reason: {type(exc).__name__}: {exc}\n\nTraceback:\n{traceback.format_exc()[:2000]}")
        raise


def run_bot_in_thread():
    thread = Thread(target=lambda: asyncio.run(bot_main()), daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    try:
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is not set. Add BOT_TOKEN to Render Environment Variables.")

        run_bot_in_thread()
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    except Exception as exc:
        log.exception("Application startup failed")
        try:
            if ADMIN_ID:
                send_admin_alert("startup_error", f"Reason: {type(exc).__name__}: {exc}\n\nTraceback:\n{traceback.format_exc()[:2000]}")
        except Exception:
            pass
        raise
