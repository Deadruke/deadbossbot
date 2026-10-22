# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from flask import Flask, jsonify

try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

# Render Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
except ValueError:
    ADMIN_ID = 0
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "nishtyakcom").strip().lstrip("@").lower()
PORT = int(os.getenv("PORT", "10000") or "10000")
# Telegram Stars use currency XTR and an EMPTY provider_token. This variable is not required.
PRO_PRICE_STARS = max(1, int(os.getenv("PRO_PRICE_STARS", "150") or "150"))
PRO_DAYS = max(1, int(os.getenv("PRO_DAYS", "30") or "30"))
MAX_VIDEO_MB = max(10, int(os.getenv("MAX_VIDEO_MB", "45") or "45"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "deadboss.db"
TMP_DIR = Path(os.getenv("TMP_DIR", str(BASE_DIR / "tmp")))
TMP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("deadbossbot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN \u043d\u0435 \u0437\u0430\u0434\u0430\u043d \u0432 Environment Variables Render")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
app = Flask(__name__)
last_admin_alerts: dict[str, float] = {}
ALERT_COOLDOWN_SECONDS = 300
MODEL_CACHE = {}
MODEL_LOCK = threading.Lock()
PROCESSING_LOCK = asyncio.Lock()
PROCESSING_OFF_TEXT = "Бот временно не работает ⚒️"
SUBTITLE_FONTS = {"sans": "DejaVu Sans", "serif": "DejaVu Serif", "mono": "DejaVu Sans Mono", "liberation": "Liberation Sans"}
SUBTITLE_COLORS = {"white": ("Белый", "&H00FFFFFF"), "yellow": ("Жёлтый", "&H0000FFFF"), "cyan": ("Голубой", "&H00FFFF00"), "green": ("Зелёный", "&H0000FF00"), "pink": ("Розовый", "&H00FF00FF")}


def is_admin(user) -> bool:
    """ADMIN_ID is authoritative; username is the fallback if ADMIN_ID is not configured."""
    if user is None:
        return False
    if ADMIN_ID > 0 and int(user.id) == ADMIN_ID:
        return True
    return (getattr(user, "username", "") or "").strip().lstrip("@").lower() == ADMIN_USERNAME


def processing_enabled() -> bool:
    return get_setting("processing_enabled", "1") == "1"


async def reject_if_processing_disabled(message: Message) -> bool:
    if processing_enabled():
        return False
    await message.answer(PROCESSING_OFF_TEXT)
    return True


def now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_db():
    with db_connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '', first_name TEXT DEFAULT '', created_at TEXT,
            stars_balance INTEGER NOT NULL DEFAULT 0,
            pro_until INTEGER NOT NULL DEFAULT 0,
            total_tasks INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, payload TEXT NOT NULL,
            amount_stars INTEGER NOT NULL, currency TEXT NOT NULL,
            charge_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, task_type TEXT, status TEXT,
            created_at TEXT, updated_at TEXT, error TEXT, output_name TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS user_states (
            user_id INTEGER PRIMARY KEY, action TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY, subtitle_font TEXT NOT NULL DEFAULT 'sans',
            subtitle_color TEXT NOT NULL DEFAULT 'white'
        );
        INSERT OR IGNORE INTO settings(key, value) VALUES ('processing_enabled', '1');
        """)
        conn.commit()


def ensure_user(user_id: int, username: str | None = None, first_name: str | None = None):
    with db_connect() as conn:
        conn.execute("""INSERT INTO users(user_id, username, first_name, created_at)
        VALUES(?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username, first_name=excluded.first_name""",
        (user_id, username or "", first_name or "", now_text()))
        conn.commit()


def set_user_action(user_id: int, action: str | None):
    with db_connect() as conn:
        if action is None:
            conn.execute("DELETE FROM user_states WHERE user_id=?", (user_id,))
        else:
            conn.execute("""INSERT INTO user_states(user_id, action, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET action=excluded.action, updated_at=excluded.updated_at""",
            (user_id, action, now_text()))
        conn.commit()


def get_user_action(user_id: int):
    with db_connect() as conn:
        row = conn.execute("SELECT action FROM user_states WHERE user_id=?", (user_id,)).fetchone()
    return row["action"] if row else None


def get_setting(key: str, default: str = "0") -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db_connect() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()


def is_pro_active(user_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT pro_until FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and int(row["pro_until"] or 0) > int(time.time()))


def set_pro_for_user(user_id: int, days: int = PRO_DAYS):
    ensure_user(user_id)
    now = int(time.time())
    with db_connect() as conn:
        row = conn.execute("SELECT pro_until FROM users WHERE user_id=?", (user_id,)).fetchone()
        current = int(row["pro_until"] or 0) if row else 0
        expiry = max(current, now) + int(days) * 86400
        conn.execute("UPDATE users SET pro_until=? WHERE user_id=?", (expiry, user_id))
        conn.commit()


def get_subtitle_preferences(user_id: int) -> tuple[str, str]:
    with db_connect() as conn:
        row = conn.execute("SELECT subtitle_font, subtitle_color FROM user_preferences WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return "sans", "white"
    font = row["subtitle_font"] if row["subtitle_font"] in SUBTITLE_FONTS else "sans"
    color = row["subtitle_color"] if row["subtitle_color"] in SUBTITLE_COLORS else "white"
    return font, color


def set_subtitle_preference(user_id: int, key: str, value: str):
    if key == "font" and value not in SUBTITLE_FONTS:
        return
    if key == "color" and value not in SUBTITLE_COLORS:
        return
    ensure_user(user_id)
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO user_preferences(user_id) VALUES(?)", (user_id,))
        column = "subtitle_font" if key == "font" else "subtitle_color"
        conn.execute(f"UPDATE user_preferences SET {column}=? WHERE user_id=?", (value, user_id))
        conn.commit()


def subtitle_settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Шрифт: Sans", callback_data="subsfont:sans"), InlineKeyboardButton(text="Serif", callback_data="subsfont:serif")],
        [InlineKeyboardButton(text="Mono", callback_data="subsfont:mono"), InlineKeyboardButton(text="Liberation", callback_data="subsfont:liberation")],
        [InlineKeyboardButton(text="Цвет: белый", callback_data="subscolor:white"), InlineKeyboardButton(text="Жёлтый", callback_data="subscolor:yellow")],
        [InlineKeyboardButton(text="Голубой", callback_data="subscolor:cyan"), InlineKeyboardButton(text="Зелёный", callback_data="subscolor:green"), InlineKeyboardButton(text="Розовый", callback_data="subscolor:pink")],
    ])


def quality_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оригинал", callback_data="quality:original"), InlineKeyboardButton(text="720p", callback_data="quality:720")],
        [InlineKeyboardButton(text="1080p", callback_data="quality:1080"), InlineKeyboardButton(text="🔥 2K PRO", callback_data="quality:2k")],
    ])


def get_user_by_username_or_id(identifier: str):
    identifier = str(identifier).strip().lstrip("@")
    with db_connect() as conn:
        if identifier.isdigit():
            row = conn.execute("SELECT * FROM users WHERE user_id=? LIMIT 1", (int(identifier),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM users WHERE lower(username)=lower(?) LIMIT 1", (identifier,)).fetchone()
    return dict(row) if row else None


def send_admin_alert(category: str, details: str):
    if not ADMIN_ID:
        return
    now = time.time()
    if now - last_admin_alerts.get(category, 0) < ALERT_COOLDOWN_SECONDS:
        return
    last_admin_alerts[category] = now
    text = f"\U0001f6a8 <b>DeadBossBot alert</b>\nTime: {now_text()}\nCategory: {category}\n\n{details}"
    async def _send():
        try:
            await bot.send_message(ADMIN_ID, text[:3900])
        except Exception:
            log.exception("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u0435 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0443")
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send())
    except RuntimeError:
        log.error("Admin alert \u0432\u043d\u0435 event loop: %s", text)


def main_keyboard(show_admin: bool = False):
    keyboard = [
        [KeyboardButton(text="🎞 Обработать видео")],
        [KeyboardButton(text="🎬 Улучшить качество"), KeyboardButton(text="💬 Добавить субтитры")],
        [KeyboardButton(text="⭐ Купить PRO"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="⚙️ Настройки субтитров")],
    ]
    if show_admin:
        keyboard.append([KeyboardButton(text="🛠 Админ")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="🔄 Обработка вкл/выкл", callback_data="admin:toggle_processing")],
        [InlineKeyboardButton(text="➕ Выдать PRO", callback_data="admin:grant_pro"), InlineKeyboardButton(text="➖ Забрать PRO", callback_data="admin:revoke_pro")],
        [InlineKeyboardButton(text="⭐ Список PRO", callback_data="admin:list_pro"), InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:list_users")],
    ])


def welcome_text():
    return ("\u041f\u0440\u0438\u0432\u0435\u0442! \u042f DeadBossBot.\n\n"
            "\U0001f3ac \u0423\u043b\u0443\u0447\u0448\u0430\u044e \u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435 \u0432\u0438\u0434\u0435\u043e \u0444\u0438\u043b\u044c\u0442\u0440\u0430\u043c\u0438.\n"
            "\U0001f4ac \u0420\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u044e \u0440\u0443\u0441\u0441\u043a\u0443\u044e \u0440\u0435\u0447\u044c \u0438 \u0434\u043e\u0431\u0430\u0432\u043b\u044f\u044e \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b.\n"
            "\u2b50 PRO \u2014 \u0437\u0430 Telegram Stars.\n\n"
            "\u0412\u044b\u0431\u0435\u0440\u0438 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u0432 \u043c\u0435\u043d\u044e, \u0437\u0430\u0442\u0435\u043c \u043e\u0442\u043f\u0440\u0430\u0432\u044c \u0432\u0438\u0434\u0435\u043e.")


@app.get("/")
def root():
    return jsonify({"status": "ok", "service": "deadbossbot"}), 200


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "deadbossbot"}), 200


@dp.message(Command("start"))
async def start(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    set_user_action(message.from_user.id, None)
    await message.answer(welcome_text(), reply_markup=main_keyboard(is_admin(message.from_user)))


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer("\u041a\u043e\u043c\u0430\u043d\u0434\u044b: /start, /help, /profile, /admin, /cancel, /grantpro ID \u0434\u043d\u0438, /revokepro ID")


async def send_profile(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    with db_connect() as conn:
        row = conn.execute("SELECT stars_balance, pro_until, total_tasks FROM users WHERE user_id=?", (user_id,)).fetchone()
    expiry = int(row["pro_until"] or 0)
    pro_text = f"\u2705 PRO \u0434\u043e {datetime.fromtimestamp(expiry, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}" if expiry > int(time.time()) else "\u274c PRO \u043d\u0435 \u0430\u043a\u0442\u0438\u0432\u0435\u043d"
    await message.answer(f"\U0001f464 <b>\u041f\u0440\u043e\u0444\u0438\u043b\u044c</b>\nID: <code>{user_id}</code>\n\u0411\u0430\u043b\u0430\u043d\u0441: {row['stars_balance']} \u2b50\n{pro_text}\n\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u043d\u043e \u0432\u0438\u0434\u0435\u043e: {row['total_tasks']}")


@dp.message(Command("profile"))
async def profile_command(message: Message):
    await send_profile(message)


async def admin_panel(message: Message):
    with db_connect() as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        pros = conn.execute("SELECT COUNT(*) c FROM users WHERE pro_until>?", (int(time.time()),)).fetchone()["c"]
        tasks = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    await message.answer(f"\U0001f6e0 <b>\u0410\u0434\u043c\u0438\u043d-\u043f\u0430\u043d\u0435\u043b\u044c</b>\n\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439: {users}\n\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0439 PRO: {pros}\n\u0417\u0430\u0434\u0430\u0447 \u0432\u0441\u0435\u0433\u043e: {tasks}\n\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430: {'\u0432\u043a\u043b\u044e\u0447\u0435\u043d\u0430' if get_setting('processing_enabled','1') == '1' else '\u0432\u044b\u043a\u043b\u044e\u0447\u0435\u043d\u0430'}", reply_markup=admin_keyboard())


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user):
        await message.answer("\U0001f6ab \u0414\u043e\u0441\u0442\u0443\u043f \u0442\u043e\u043b\u044c\u043a\u043e \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0443.")
        return
    await admin_panel(message)


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    action = callback.data
    if action == "admin:toggle_processing":
        new = "0" if processing_enabled() else "1"
        set_setting("processing_enabled", new)
        if callback.message:
            await callback.message.answer(f"Обработка {'включена' if new == '1' else 'выключена'}.", reply_markup=admin_keyboard())
    elif action == "admin:stats":
        if callback.message:
            await admin_panel(callback.message)
    elif action == "admin:list_users":
        with db_connect() as conn:
            rows = conn.execute("SELECT user_id,username,stars_balance,pro_until FROM users ORDER BY user_id DESC LIMIT 15").fetchall()
        body = "\n".join(f"{r['user_id']} | @{r['username'] or '—'} | {'PRO' if int(r['pro_until'] or 0) > int(time.time()) else '—'} | {r['stars_balance']} ⭐" for r in rows)
        if callback.message:
            await callback.message.answer(("👥 <b>Последние пользователи</b>\n" + (body or "Пользователей пока нет."))[:3900])
    elif action == "admin:list_pro":
        with db_connect() as conn:
            rows = conn.execute("SELECT user_id,username,pro_until FROM users WHERE pro_until>? ORDER BY pro_until DESC LIMIT 50", (int(time.time()),)).fetchall()
        body = "\n".join(f"{r['user_id']} | @{r['username'] or '—'} | до {datetime.fromtimestamp(int(r['pro_until']), timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}" for r in rows)
        if callback.message:
            await callback.message.answer(("⭐ <b>Активные PRO-пользователи</b>\n" + (body or "Активных PRO-подписок нет."))[:3900])
    elif action in {"admin:grant_pro", "admin:revoke_pro"}:
        state = "admin_grantpro" if action.endswith("grant_pro") else "admin_revokepro"
        set_user_action(callback.from_user.id, state)
        prompt = ("Отправь ID или @username пользователя и количество дней. Пример: <code>@username 30</code> или <code>123456789 30</code>."
                  if state == "admin_grantpro" else
                  "Отправь ID или @username пользователя, у которого нужно забрать PRO.")
        if callback.message:
            await callback.message.answer(prompt)
    await callback.answer()


@dp.message(Command("grantpro"))
async def grantpro(message: Message):
    if not is_admin(message.from_user):
        await message.answer("⛔ Доступ только владельцу.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        set_user_action(message.from_user.id, "admin_grantpro")
        await message.answer("Отправь ID или @username пользователя и количество дней. Пример: <code>@username 30</code>.")
        return
    identifier = parts[1]
    days = PRO_DAYS
    if len(parts) >= 3 and parts[2].isdigit():
        days = int(parts[2])
    if days < 1 or days > 3650:
        await message.answer("Количество дней должно быть от 1 до 3650.")
        return
    target = get_user_by_username_or_id(identifier)
    if not target and identifier.lstrip("@").isdigit():
        target_id = int(identifier.lstrip("@"))
        ensure_user(target_id)
        target = {"user_id": target_id}
    if not target:
        await message.answer("Пользователь по этому username не найден. Он должен хотя бы раз запустить бота через /start. Для ID можно выдать PRO напрямую.")
        return
    set_pro_for_user(int(target["user_id"]), days)
    await message.answer(f"✅ PRO выдан пользователю <code>{target['user_id']}</code> на {days} дн.")


@dp.message(Command("revokepro"))
async def revokepro(message: Message):
    if not is_admin(message.from_user):
        await message.answer("⛔ Доступ только владельцу.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        set_user_action(message.from_user.id, "admin_revokepro")
        await message.answer("Отправь ID или @username пользователя.")
        return
    target = get_user_by_username_or_id(parts[1])
    if not target:
        await message.answer("Пользователь не найден. По username можно найти только тех, кто уже запускал бота.")
        return
    with db_connect() as conn:
        conn.execute("UPDATE users SET pro_until=0 WHERE user_id=?", (target["user_id"],))
        conn.commit()
    await message.answer(f"✅ PRO отключён у пользователя <code>{target['user_id']}</code>.")


@dp.message(Command("cancel"))
async def cancel(message: Message):
    set_user_action(message.from_user.id, None)
    await message.answer("\U0001f6d1 \u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.", reply_markup=main_keyboard(is_admin(message.from_user)))


@dp.message(F.text == "🎞 Обработать видео")
async def banner_button(message: Message):
    if await reject_if_processing_disabled(message):
        return
    if find_banner() is None:
        await message.answer("Не найден файл баннера. Добавь его в assets/banner.mp4 или assets/banner.png.MP4, либо задай BANNER_PATH в Render.")
        return
    set_user_action(message.from_user.id, "banner")
    await message.answer("Отправь видео. Баннер будет наложен на первые 7 секунд, зелёный фон будет удалён. Для отмены — /cancel.")


@dp.message(F.text == "\U0001f3ac \u0423\u043b\u0443\u0447\u0448\u0438\u0442\u044c \u043a\u0430\u0447\u0435\u0441\u0442\u0432\u043e")
async def quality_button(message: Message):
    if await reject_if_processing_disabled(message):
        return
    set_user_action(message.from_user.id, None)
    await message.answer("Выбери качество. 2K доступно только с активным PRO:", reply_markup=quality_keyboard())


@dp.message(F.text == "\u2699\ufe0f \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u043e\u0432")
async def subtitle_settings_button(message: Message):
    user_id = message.from_user.id
    if not is_pro_active(user_id):
        await message.answer("⚙️ Выбор шрифта и цвета субтитров доступен только с активным PRO. Обычные субтитры по-прежнему бесплатны.")
        return
    font, color = get_subtitle_preferences(user_id)
    await message.answer(f"Настройки субтитров PRO\nШрифт: {SUBTITLE_FONTS[font]}\nЦвет: {SUBTITLE_COLORS[color][0]}\nВыбери, что изменить:", reply_markup=subtitle_settings_keyboard())


@dp.callback_query(F.data.startswith("quality:"))
async def quality_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not processing_enabled():
        if callback.message:
            await callback.message.answer(PROCESSING_OFF_TEXT)
        await callback.answer()
        return
    quality = callback.data.split(":", 1)[1]
    if quality not in {"original", "720", "1080", "2k"}:
        await callback.answer("Неизвестное качество", show_alert=True)
        return
    if quality == "2k" and not is_pro_active(user_id):
        await callback.answer("2K доступно только с активным PRO", show_alert=True)
        return
    set_user_action(user_id, f"quality:{quality}")
    labels = {"original": "оригинальное разрешение", "720": "720p", "1080": "1080p", "2k": "2K (PRO)"}
    await callback.answer()
    if callback.message:
        await callback.message.answer(f"Выбрано: {labels[quality]}. Теперь отправь видео файлом или обычным видео. Для отмены — /cancel.")


@dp.callback_query(F.data.startswith("subsfont:"))
async def subtitle_font_callback(callback: CallbackQuery):
    if not is_pro_active(callback.from_user.id):
        await callback.answer("Настройки доступны только с PRO", show_alert=True)
        return
    value = callback.data.split(":", 1)[1]
    if value not in SUBTITLE_FONTS:
        await callback.answer("Неизвестный шрифт", show_alert=True)
        return
    set_subtitle_preference(callback.from_user.id, "font", value)
    await callback.answer(f"Шрифт: {SUBTITLE_FONTS[value]}")


@dp.callback_query(F.data.startswith("subscolor:"))
async def subtitle_color_callback(callback: CallbackQuery):
    if not is_pro_active(callback.from_user.id):
        await callback.answer("Настройки доступны только с PRO", show_alert=True)
        return
    value = callback.data.split(":", 1)[1]
    if value not in SUBTITLE_COLORS:
        await callback.answer("Неизвестный цвет", show_alert=True)
        return
    set_subtitle_preference(callback.from_user.id, "color", value)
    await callback.answer(f"Цвет: {SUBTITLE_COLORS[value][0]}")


@dp.message(F.text == "\U0001f4ac \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b")
async def subtitles_button(message: Message):
    if await reject_if_processing_disabled(message):
        return
    set_user_action(message.from_user.id, "subtitles")
    await message.answer("\U0001f3ac \u041f\u0440\u0438\u0448\u043b\u0438 \u0432\u0438\u0434\u0435\u043e \u0441 \u0440\u0435\u0447\u044c\u044e. \u042f \u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u044e \u0440\u0443\u0441\u0441\u043a\u0443\u044e \u0440\u0435\u0447\u044c \u0438 \u0432\u0441\u0442\u0440\u043e\u044e \u0436\u0438\u0440\u043d\u044b\u0435 \u0431\u0435\u043b\u044b\u0435 \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u0441 \u0447\u0451\u0440\u043d\u043e\u0439 \u043e\u0431\u0432\u043e\u0434\u043a\u043e\u0439 \u0447\u0443\u0442\u044c \u043d\u0438\u0436\u0435 \u0441\u0435\u0440\u0435\u0434\u0438\u043d\u044b \u043a\u0430\u0434\u0440\u0430. \u0414\u043b\u044f \u043e\u0442\u043c\u0435\u043d\u044b \u2014 /cancel.")


@dp.message(F.text == "\u2b50 \u041a\u0443\u043f\u0438\u0442\u044c PRO")
async def buy_pro(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    if is_pro_active(user_id):
        await message.answer("\u2705 PRO \u0443 \u0442\u0435\u0431\u044f \u0443\u0436\u0435 \u0430\u043a\u0442\u0438\u0432\u0435\u043d.")
        return
    payload = f"deadboss:pro:{PRO_DAYS}:{user_id}:{uuid.uuid4().hex}"
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="DeadBossBot PRO",
            description=f"\u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 PRO \u043d\u0430 {PRO_DAYS} \u0434\u043d\u0435\u0439.",
            payload=payload,
            provider_token="",  # Required for Telegram Stars: empty string, not a provider token.
            currency="XTR",
            prices=[LabeledPrice(label=f"PRO \u043d\u0430 {PRO_DAYS} \u0434\u043d\u0435\u0439", amount=PRO_PRICE_STARS)],
        )
    except Exception as exc:
        log.exception("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043e\u0437\u0434\u0430\u0442\u044c \u0441\u0447\u0451\u0442 Telegram Stars")
        await message.answer(f"\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043e\u0437\u0434\u0430\u0442\u044c \u0441\u0447\u0451\u0442. \u041f\u0440\u043e\u0432\u0435\u0440\u044c \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0431\u043e\u0442\u0430 \u0438 \u043f\u043e\u043f\u0440\u043e\u0431\u0443\u0439 \u043f\u043e\u0437\u0436\u0435.\n\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {type(exc).__name__}")


@dp.message(F.text == "\U0001f464 \u041f\u0440\u043e\u0444\u0438\u043b\u044c")
async def profile_button(message: Message):
    await send_profile(message)


@dp.message(F.text == "\U0001f6e0 \u0410\u0434\u043c\u0438\u043d")
async def admin_button(message: Message):
    if not is_admin(message.from_user):
        await message.answer("\U0001f6ab \u0414\u043e\u0441\u0442\u0443\u043f \u0442\u043e\u043b\u044c\u043a\u043e \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0443.")
        return
    await admin_panel(message)


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    # Validate invoice payload, owner and exact Stars amount before approving checkout.
    try:
        parts = query.invoice_payload.split(":")
        valid = (len(parts) == 5 and parts[0] == "deadboss" and parts[1] == "pro"
                 and parts[2].isdigit() and int(parts[2]) == PRO_DAYS
                 and parts[3].isdigit() and int(parts[3]) == query.from_user.id
                 and query.currency == "XTR" and query.total_amount == PRO_PRICE_STARS)
    except Exception:
        valid = False
    await bot.answer_pre_checkout_query(query.id, ok=valid, error_message=None if valid else "\u0421\u0447\u0451\u0442 \u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u0435\u043d. \u041e\u0442\u043a\u0440\u043e\u0439 \u043f\u043e\u043a\u0443\u043f\u043a\u0443 PRO \u0437\u0430\u043d\u043e\u0432\u043e.")


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    parts = (payment.invoice_payload or "").split(":")
    if not (len(parts) == 5 and parts[0] == "deadboss" and parts[1] == "pro"
            and parts[3].isdigit() and int(parts[3]) == user_id
            and payment.currency == "XTR" and payment.total_amount == PRO_PRICE_STARS):
        log.error("\u041f\u043e\u043b\u0443\u0447\u0435\u043d \u043f\u043b\u0430\u0442\u0451\u0436 \u0441 \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u043c payload \u043e\u0442 user=%s", user_id)
        await message.answer("\u26a0\ufe0f \u041f\u043b\u0430\u0442\u0451\u0436 \u043f\u043e\u043b\u0443\u0447\u0435\u043d, \u043d\u043e \u0441\u0447\u0451\u0442 \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438. \u0421\u0432\u044f\u0436\u0438\u0441\u044c \u0441 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u043e\u043c.")
        send_admin_alert("payment_validation", f"\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c {user_id}; payload={payment.invoice_payload!r}; currency={payment.currency}; amount={payment.total_amount}")
        return
    charge_id = payment.telegram_payment_charge_id
    if not charge_id:
        await message.answer("\u26a0\ufe0f \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u0438\u0434\u0435\u043d\u0442\u0438\u0444\u0438\u043a\u0430\u0442\u043e\u0440 \u043f\u043b\u0430\u0442\u0435\u0436\u0430. \u0421\u0432\u044f\u0436\u0438\u0441\u044c \u0441 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u043e\u043c.")
        return

    # Insert charge and extend PRO in one DB transaction. UNIQUE charge_id prevents duplicate credit.
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT id FROM payments WHERE charge_id=?", (charge_id,)).fetchone()
        if exists:
            conn.rollback()
            await message.answer("\u2705 \u042d\u0442\u043e\u0442 \u043f\u043b\u0430\u0442\u0451\u0436 \u0443\u0436\u0435 \u0443\u0447\u0442\u0451\u043d. PRO \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e \u043d\u0435 \u043d\u0430\u0447\u0438\u0441\u043b\u044f\u043b\u0441\u044f.")
            return
        conn.execute("INSERT INTO payments(user_id,payload,amount_stars,currency,charge_id,status,created_at) VALUES(?,?,?,?,?,'paid',?)",
                     (user_id, payment.invoice_payload, int(payment.total_amount), payment.currency, charge_id, now_text()))
        row = conn.execute("SELECT pro_until FROM users WHERE user_id=?", (user_id,)).fetchone()
        now = int(time.time())
        current = int(row["pro_until"] or 0) if row else 0
        expiry = max(current, now) + PRO_DAYS * 86400
        conn.execute("UPDATE users SET pro_until=? WHERE user_id=?", (expiry, user_id))
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u043f\u0438\u0441\u0430\u0442\u044c \u043f\u043b\u0430\u0442\u0451\u0436/PRO")
        send_admin_alert("payment_db_error", f"User {user_id}; charge {charge_id}")
        await message.answer("\u26a0\ufe0f Telegram \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u043b \u043f\u043b\u0430\u0442\u0451\u0436, \u043d\u043e \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 PRO \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c. \u0410\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440 \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0451\u043d; \u043d\u0435 \u043e\u043f\u043b\u0430\u0447\u0438\u0432\u0430\u0439 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u043e.")
        return
    finally:
        conn.close()
    await message.answer(f"\U0001f389 \u041e\u043f\u043b\u0430\u0442\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430! PRO \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043d \u043d\u0430 {PRO_DAYS} \u0434\u043d\u0435\u0439 \u0437\u0430 {PRO_PRICE_STARS} \u2b50.")


def run_command(command: list[str], timeout: int | None = None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("На Render не найден FFmpeg/FFprobe. Проверь apt.txt и сделай новый deploy.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Обработка превысила допустимое время. Попробуй более короткое видео.") from exc
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "Неизвестная ошибка FFmpeg").strip()
        raise RuntimeError(error[-1800:])
    return result.stdout.strip()


def probe_video(path: Path) -> tuple[int, int]:
    output = run_command(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)], timeout=30)
    match = re.match(r"(\d+)x(\d+)", output)
    if not match:
        raise RuntimeError("Не удалось определить размер видео.")
    return int(match.group(1)), int(match.group(2))


def even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def get_output_size(input_path: Path, quality: str) -> tuple[int, int]:
    width, height = probe_video(input_path)
    if quality == "original":
        return even(width), even(height)
    target_long = {"720": 1280, "1080": 1920, "2k": 2560}.get(quality)
    if target_long is None:
        raise RuntimeError("Неизвестное качество видео.")
    if width >= height:
        return target_long, even(target_long * height / width)
    return even(target_long * width / height), target_long


def find_banner() -> Path | None:
    configured = os.getenv("BANNER_PATH", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend([
        BASE_DIR / "assets" / "banner.mp4",
        BASE_DIR / "assets" / "banner.png.MP4",
        BASE_DIR / "assets" / "banner.MP4",
    ])
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def video_encode_command(input_path: Path, output_path: Path, video_filter: str, preset: str, crf: int, timeout: int, banner_width: int | None = None) -> list[str]:
    banner = find_banner()
    if banner is None:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
            "-vf", video_filter, "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_path)
        ]

    width = banner_width or 320
    # The banner is keyed on green and overlaid at the top-right for its first seven seconds.
    complex_filter = (
        f"[0:v]{video_filter}[base];"
        f"[1:v]chromakey=0x00FF00:0.18:0.08,scale={width}:-2:flags=lanczos,format=rgba[ban];"
        "[base][ban]overlay=W-w-20:20:enable='between(t,0,7)'[outv]"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
        "-stream_loop", "-1", "-i", str(banner), "-filter_complex", complex_filter,
        "-map", "[outv]", "-map", "0:a?", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(output_path)
    ]


def improve_video(input_path: Path, output_path: Path, quality: str = "original", pro: bool = False):
    width, height = get_output_size(input_path, quality)
    preset = "ultrafast" if pro else "veryfast"
    filters = f"scale={width}:{height}:flags=lanczos,hqdn3d=1.2:1.2:4:4,eq=contrast=1.08:saturation=1.07:brightness=0.004,unsharp=5:5:0.60:3:3:0.0,format=yuv420p"
    banner_width = max(180, even(width * 0.28))
    run_command(video_encode_command(input_path, output_path, filters, preset, 21, 1800, banner_width), timeout=1800)


def get_whisper_model(model_name: str):
    if WhisperModel is None:
        raise RuntimeError("Не установлена faster-whisper. Проверь requirements.txt и deploy.")
    with MODEL_LOCK:
        model = MODEL_CACHE.get(model_name)
        if model is None:
            log.info("Загружаю модель распознавания речи: %s", model_name)
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            MODEL_CACHE[model_name] = model
        return model


def transcribe_to_srt(input_path: Path, work_dir: Path) -> Path:
    # Mono 16 kHz FLAC reduces temporary storage/I/O without losing audio quality.
    audio_path = work_dir / "audio.flac"
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", "-compression_level", "3", str(audio_path)
    ], timeout=600)
    if not audio_path.exists() or audio_path.stat().st_size < 1000:
        raise RuntimeError("В видео не найден слышимый аудиотрек. Проверь, что в нём есть речь.")

    model_name = os.getenv("WHISPER_MODEL", "base").strip() or "base"
    language = os.getenv("WHISPER_LANGUAGE", "ru").strip() or "ru"
    model = get_whisper_model(model_name)
    segments, info = model.transcribe(
        str(audio_path), language=language, beam_size=1, best_of=1,
        vad_filter=True, condition_on_previous_text=False,
        vad_parameters={"min_silence_duration_ms": 350}
    )
    srt_path = work_dir / "subtitles.srt"

    def stamp(seconds: float) -> str:
        ms = max(0, int(seconds * 1000))
        hours, ms = divmod(ms, 3_600_000)
        minutes, ms = divmod(ms, 60_000)
        secs, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"

    count = 0
    with srt_path.open("w", encoding="utf-8-sig", newline="\n") as file:
        for seg in segments:
            text = " ".join((seg.text or "").split()).replace("-->", "—>")
            if not text:
                continue
            count += 1
            start = max(0.0, float(seg.start))
            end = max(float(seg.end), start + 0.15)
            file.write(f"{count}\n{stamp(start)} --> {stamp(end)}\n{text}\n\n")
    log.info("Subtitle transcription completed: %s segments; language=%s; model=%s", count, getattr(info, "language", language), model_name)
    if count == 0:
        raise RuntimeError("Речь не распознана. Проверь громкость и качество звука; попробуй ролик с чёткой речью.")
    return srt_path


def add_subtitles(input_path: Path, output_path: Path, work_dir: Path, user_id: int, pro: bool):
    width, height = probe_video(input_path)
    srt_path = transcribe_to_srt(input_path, work_dir)
    font_key, color_key = get_subtitle_preferences(user_id) if pro else ("sans", "white")
    font_name = SUBTITLE_FONTS.get(font_key, "DejaVu Sans")
    color = SUBTITLE_COLORS.get(color_key, SUBTITLE_COLORS["white"])[1]
    font_size = max(22, min(54, int(height * 0.047)))
    # ASS/SSA colours use AABBGGRR. The subtitles filter renders text directly into the output.
    margin_v = max(20, int(height * 0.38))
    srt_path_filter = str(srt_path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    font_name = font_name.replace("'", "")
    style = (
        f"FontName={font_name},FontSize={font_size},Bold=1,PrimaryColour={color},"
        "SecondaryColour=&H000000FF,OutlineColour=&H00000000,BackColour=&H64000000,"
        f"BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV={margin_v}"
    )
    preset = "ultrafast" if pro else "veryfast"
    filters = f"hqdn3d=0.8:0.8:3:3,eq=contrast=1.04:saturation=1.04,subtitles='{srt_path_filter}':force_style='{style}',format=yuv420p"
    banner_width = max(180, even(width * 0.28))
    run_command(video_encode_command(input_path, output_path, filters, preset, 22, 2400, banner_width), timeout=2400)


async def _process_video_impl(message: Message, action: str):
    user_id = message.from_user.id
    file_obj = message.video or message.document
    if file_obj is None:
        await message.answer("\u041f\u0440\u0438\u0448\u043b\u0438 \u0432\u0438\u0434\u0435\u043e, \u043f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430.")
        return
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("\u042d\u0442\u043e \u043d\u0435 \u043f\u043e\u0445\u043e\u0436\u0435 \u043d\u0430 \u0432\u0438\u0434\u0435\u043e\u0444\u0430\u0439\u043b. \u041e\u0442\u043f\u0440\u0430\u0432\u044c \u0432\u0438\u0434\u0435\u043e \u043a\u0430\u043a \u0432\u0438\u0434\u0435\u043e \u0438\u043b\u0438 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442 \u0441 \u0442\u0438\u043f\u043e\u043c video/.")
        return
    file_size = getattr(file_obj, "file_size", None) or 0
    if file_size and file_size > MAX_VIDEO_MB * 1024 * 1024:
        await message.answer(f"\u0424\u0430\u0439\u043b \u0441\u043b\u0438\u0448\u043a\u043e\u043c \u0431\u043e\u043b\u044c\u0448\u043e\u0439. \u041c\u0430\u043a\u0441\u0438\u043c\u0430\u043b\u044c\u043d\u044b\u0439 \u0440\u0430\u0437\u043c\u0435\u0440 \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0431\u043e\u0442\u0430: {MAX_VIDEO_MB} \u041c\u0411.")
        set_user_action(user_id, None)
        return

    pro = is_pro_active(user_id)
    quality = action.split(":", 1)[1] if action.startswith("quality:") else "original"
    if quality == "2k" and not pro:
        set_user_action(user_id, None)
        await message.answer("2K доступно только с активным PRO. Выбери другое качество.")
        return

    work_dir = TMP_DIR / f"job_{user_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        await message.answer("\U0001f4e5 \u0421\u043a\u0430\u0447\u0438\u0432\u0430\u044e \u0432\u0438\u0434\u0435\u043e\u2026")
        file_info = await bot.get_file(file_obj.file_id)
        original_name = getattr(file_obj, "file_name", None) or "input.mp4"
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
            suffix = ".mp4"
        source = work_dir / f"input{suffix}"
        await bot.download_file(file_info.file_path, destination=source)
        if not source.exists() or source.stat().st_size == 0:
            raise RuntimeError("Telegram \u043d\u0435 \u0441\u043a\u0430\u0447\u0430\u043b \u0432\u0438\u0434\u0435\u043e \u0438\u043b\u0438 \u0444\u0430\u0439\u043b \u043f\u0443\u0441\u0442\u043e\u0439.")
        output = work_dir / "processed.mp4"
        if action.startswith("quality:"):
            await message.answer(f"🛠 Обрабатываю видео ({quality.upper()}).")
            await asyncio.to_thread(improve_video, source, output, quality, pro)
            label = {"original": "оригинальное разрешение", "720": "720p", "1080": "1080p", "2k": "2K"}.get(quality, quality)
            caption = f"✅ Готово! Видео обработано в режиме {label}. Масштабирование не восстанавливает детали, которых не было в оригинале."
        elif action == "banner":
            banner = find_banner()
            if banner is None:
                raise RuntimeError("Не найден файл баннера. Добавь assets/banner.mp4 или assets/banner.png.MP4 либо задай BANNER_PATH.")
            width, _ = probe_video(source)
            filters = "format=yuv420p"
            await message.answer("🎞 Накладываю баннер и удаляю зелёный фон…")
            await asyncio.to_thread(run_command, video_encode_command(source, output, filters, "veryfast", 22, 1800, max(180, even(width * 0.28))), 1800)
            caption = "✅ Готово! Баннер добавлен на первые 7 секунд, зелёный фон удалён."
        elif action == "subtitles":
            await message.answer("📝 Распознаю речь и встраиваю субтитры. На первом запуске может потребоваться больше времени для загрузки модели.")
            await asyncio.to_thread(add_subtitles, source, output, work_dir, user_id, pro)
            font_key, color_key = get_subtitle_preferences(user_id) if pro else ("sans", "white")
            caption = f"✅ Готово! Субтитры встроены. Шрифт: {SUBTITLE_FONTS[font_key]}; цвет: {SUBTITLE_COLORS[color_key][0]}."
        else:
            raise RuntimeError("\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 \u0440\u0435\u0436\u0438\u043c \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438. \u041d\u0430\u0436\u043c\u0438 \u043a\u043d\u043e\u043f\u043a\u0443 \u0432 \u043c\u0435\u043d\u044e \u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u044c \u0432\u0438\u0434\u0435\u043e \u0437\u0430\u043d\u043e\u0432\u043e.")
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg \u043d\u0435 \u0441\u043e\u0437\u0434\u0430\u043b \u0433\u043e\u0442\u043e\u0432\u044b\u0439 \u0432\u0438\u0434\u0435\u043e\u0444\u0430\u0439\u043b.")
        if output.stat().st_size > 49 * 1024 * 1024:
            raise RuntimeError("\u0413\u043e\u0442\u043e\u0432\u043e\u0435 \u0432\u0438\u0434\u0435\u043e \u043f\u0440\u0435\u0432\u044b\u0448\u0430\u0435\u0442 \u043b\u0438\u043c\u0438\u0442 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438 Telegram \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0431\u043e\u0442\u0430. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439 \u0431\u043e\u043b\u0435\u0435 \u043a\u043e\u0440\u043e\u0442\u043a\u043e\u0435 \u0432\u0438\u0434\u0435\u043e.")
        await message.answer("\U0001f4e4 \u041e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u044e \u0433\u043e\u0442\u043e\u0432\u043e\u0435 \u0432\u0438\u0434\u0435\u043e\u2026")
        await bot.send_video(message.chat.id, FSInputFile(str(output)), caption=caption, supports_streaming=True)
        with db_connect() as conn:
            conn.execute("UPDATE users SET total_tasks=total_tasks+1 WHERE user_id=?", (user_id,))
            conn.execute("INSERT INTO tasks(user_id,task_type,status,created_at,updated_at,output_name) VALUES(?,?,'done',?,?,?)", (user_id, action.split(":", 1)[0], now_text(), now_text(), output.name))
            conn.commit()
    except Exception as exc:
        log.exception("\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438 \u0432\u0438\u0434\u0435\u043e")
        with db_connect() as conn:
            conn.execute("INSERT INTO tasks(user_id,task_type,status,created_at,updated_at,error) VALUES(?,?,'error',?,?,?)", (user_id, action.split(":", 1)[0], now_text(), now_text(), str(exc)[:1000]))
            conn.commit()
        send_admin_alert("video_processing_error", f"\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c: {user_id}\n\u041e\u0448\u0438\u0431\u043a\u0430: {type(exc).__name__}: {exc}")
        reason = str(exc).replace("<", "&lt;").replace(">", "&gt;")[:1200]
        await message.answer(f"\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u0442\u044c \u0432\u0438\u0434\u0435\u043e.\n<code>{reason}</code>\n\n\u0415\u0441\u043b\u0438 \u043e\u0448\u0438\u0431\u043a\u0430 \u043f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0441\u044f, \u043f\u0440\u0438\u0448\u043b\u0438 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0443 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u0441\u0442\u0440\u043e\u043a\u0438 Render Logs.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        set_user_action(user_id, None)


async def process_video(message: Message, action: str):
    # Only one video runs at a time. Queued tasks re-check the persistent switch after acquiring the lock.
    if not processing_enabled():
        set_user_action(message.from_user.id, None)
        await message.answer(PROCESSING_OFF_TEXT)
        return
    async with PROCESSING_LOCK:
        if not processing_enabled():
            set_user_action(message.from_user.id, None)
            await message.answer(PROCESSING_OFF_TEXT)
            return
        await _process_video_impl(message, action)


@dp.message(F.video | F.document)
async def media_handler(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    if not processing_enabled():
        await message.answer(PROCESSING_OFF_TEXT)
        return
    action = get_user_action(user_id)
    if (action and action.startswith("quality:")) or action in {"subtitles", "banner"}:
        await process_video(message, action)
        return
    await message.answer("\u0412\u0438\u0434\u0435\u043e \u043f\u043e\u043b\u0443\u0447\u0438\u043b! \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u044b\u0431\u0435\u0440\u0438 \u0432 \u043c\u0435\u043d\u044e \u00ab\U0001f3ac \u0423\u043b\u0443\u0447\u0448\u0438\u0442\u044c \u043a\u0430\u0447\u0435\u0441\u0442\u0432\u043e\u00bb \u0438\u043b\u0438 \u00ab\U0001f4ac \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b\u00bb, \u0437\u0430\u0442\u0435\u043c \u043e\u0442\u043f\u0440\u0430\u0432\u044c \u0432\u0438\u0434\u0435\u043e \u0435\u0449\u0451 \u0440\u0430\u0437.", reply_markup=main_keyboard(is_admin(message.from_user)))


@dp.message(F.text)
async def fallback_text(message: Message):
    raw = (message.text or "").strip()
    state = get_user_action(message.from_user.id)

    if state in {"admin_grantpro", "admin_revokepro"}:
        if not is_admin(message.from_user):
            set_user_action(message.from_user.id, None)
            await message.answer("⛔ Доступ запрещён.")
            return
        parts = raw.split()
        if not parts:
            await message.answer("Отправь ID или @username.")
            return
        identifier = parts[0]
        target = get_user_by_username_or_id(identifier)
        if not target and identifier.lstrip("@").isdigit() and state == "admin_grantpro":
            target_id = int(identifier.lstrip("@"))
            ensure_user(target_id)
            target = {"user_id": target_id}
        if not target:
            await message.answer("Пользователь не найден. По username можно найти только тех, кто уже запускал бота. Попробуй снова или /cancel.")
            return
        target_id = int(target["user_id"])
        if state == "admin_grantpro":
            days = PRO_DAYS
            if len(parts) > 1:
                if not parts[1].isdigit():
                    await message.answer("Количество дней должно быть числом. Пример: @username 30")
                    return
                days = int(parts[1])
            if days < 1 or days > 3650:
                await message.answer("Количество дней должно быть от 1 до 3650.")
                return
            set_pro_for_user(target_id, days)
            await message.answer(f"✅ PRO выдан пользователю <code>{target_id}</code> на {days} дн.", reply_markup=admin_keyboard())
        else:
            with db_connect() as conn:
                conn.execute("UPDATE users SET pro_until=0 WHERE user_id=?", (target_id,))
                conn.commit()
            await message.answer(f"✅ PRO отключён у пользователя <code>{target_id}</code>.", reply_markup=admin_keyboard())
        set_user_action(message.from_user.id, None)
        return

    text = raw.lower()
    if text in {"привет", "ку", "hello", "hi", "start"}:
        await message.answer(welcome_text(), reply_markup=main_keyboard(is_admin(message.from_user)))
    else:
        await message.answer("Выбери действие кнопкой меню или используй /help.", reply_markup=main_keyboard(is_admin(message.from_user)))

async def bot_main():
    ensure_db()
    log.info("Starting DeadBossBot polling")
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False)


def bot_thread_main():
    try:
        asyncio.run(bot_main())
    except Exception as exc:
        log.exception("Telegram polling \u0437\u0430\u0432\u0435\u0440\u0448\u0438\u043b\u0441\u044f \u0441 \u043e\u0448\u0438\u0431\u043a\u043e\u0439")
        # Can't safely send async alerts here after the loop has closed; Render Logs contain traceback.


if __name__ == "__main__":
    ensure_db()
    thread = threading.Thread(target=bot_thread_main, name="telegram-polling", daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
