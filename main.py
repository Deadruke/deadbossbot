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
import traceback
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
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


def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="\U0001f3ac \u0423\u043b\u0443\u0447\u0448\u0438\u0442\u044c \u043a\u0430\u0447\u0435\u0441\u0442\u0432\u043e"), KeyboardButton(text="\U0001f4ac \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b")],
        [KeyboardButton(text="\u2b50 \u041a\u0443\u043f\u0438\u0442\u044c PRO"), KeyboardButton(text="\U0001f464 \u041f\u0440\u043e\u0444\u0438\u043b\u044c")],
        [KeyboardButton(text="\U0001f6e0 \u0410\u0434\u043c\u0438\u043d")],
    ], resize_keyboard=True)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4ca \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430", callback_data="admin:stats"), InlineKeyboardButton(text="\U0001f504 \u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u0432\u043a\u043b/\u0432\u044b\u043a\u043b", callback_data="admin:toggle_processing")],
        [InlineKeyboardButton(text="\U0001f465 \u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0438", callback_data="admin:list_users")],
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
    await message.answer(welcome_text(), reply_markup=main_keyboard())


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
    if message.from_user.id != ADMIN_ID:
        await message.answer("\U0001f6ab \u0414\u043e\u0441\u0442\u0443\u043f \u0442\u043e\u043b\u044c\u043a\u043e \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0443.")
        return
    await admin_panel(message)


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("\u0414\u043e\u0441\u0442\u0443\u043f \u0437\u0430\u043f\u0440\u0435\u0449\u0451\u043d", show_alert=True)
        return
    if callback.data == "admin:toggle_processing":
        new = "0" if get_setting("processing_enabled", "1") == "1" else "1"
        set_setting("processing_enabled", new)
        if callback.message:
            await callback.message.answer(f"\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 {'\u0432\u043a\u043b\u044e\u0447\u0435\u043d\u0430' if new == '1' else '\u0432\u044b\u043a\u043b\u044e\u0447\u0435\u043d\u0430'}.")
    elif callback.data == "admin:stats":
        if callback.message:
            await admin_panel(callback.message)
    elif callback.data == "admin:list_users":
        with db_connect() as conn:
            rows = conn.execute("SELECT user_id,username,stars_balance,pro_until FROM users ORDER BY user_id DESC LIMIT 10").fetchall()
        text = "\U0001f465 <b>\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0438</b>\n" + "\n".join(f"{r['user_id']} | @{r['username'] or '\u2014'} | {'PRO' if r['pro_until'] > int(time.time()) else '\u2014'} | {r['stars_balance']} \u2b50" for r in rows)
        if callback.message:
            await callback.message.answer(text[:3900])
    await callback.answer()


@dp.message(Command("grantpro"))
async def grantpro(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("\U0001f6ab \u0414\u043e\u0441\u0442\u0443\u043f \u0442\u043e\u043b\u044c\u043a\u043e \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0443.")
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[2].isdigit() or int(parts[2]) < 1:
        await message.answer("\u0424\u043e\u0440\u043c\u0430\u0442: /grantpro USER_ID \u0414\u041d\u0418")
        return
    target = get_user_by_username_or_id(parts[1])
    if not target and parts[1].isdigit():
        ensure_user(int(parts[1]))
        target = {"user_id": int(parts[1])}
    if not target:
        await message.answer("\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d. \u041f\u0443\u0441\u0442\u044c \u0441\u043d\u0430\u0447\u0430\u043b\u0430 \u043d\u0430\u0436\u043c\u0451\u0442 /start.")
        return
    set_pro_for_user(int(target["user_id"]), int(parts[2]))
    await message.answer(f"\u2705 PRO \u0432\u044b\u0434\u0430\u043d \u043d\u0430 {parts[2]} \u0434\u043d\u0435\u0439.")


@dp.message(Command("revokepro"))
async def revokepro(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("\U0001f6ab \u0414\u043e\u0441\u0442\u0443\u043f \u0442\u043e\u043b\u044c\u043a\u043e \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0443.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("\u0424\u043e\u0440\u043c\u0430\u0442: /revokepro USER_ID")
        return
    target = get_user_by_username_or_id(parts[1])
    if not target:
        await message.answer("\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d.")
        return
    with db_connect() as conn:
        conn.execute("UPDATE users SET pro_until=0 WHERE user_id=?", (target["user_id"],))
        conn.commit()
    await message.answer("\u2705 PRO \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d.")


@dp.message(Command("cancel"))
async def cancel(message: Message):
    set_user_action(message.from_user.id, None)
    await message.answer("\U0001f6d1 \u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.", reply_markup=main_keyboard())


@dp.message(F.text == "\U0001f3ac \u0423\u043b\u0443\u0447\u0448\u0438\u0442\u044c \u043a\u0430\u0447\u0435\u0441\u0442\u0432\u043e")
async def quality_button(message: Message):
    set_user_action(message.from_user.id, "quality")
    await message.answer("\U0001f4f9 \u041f\u0440\u0438\u0448\u043b\u0438 \u0432\u0438\u0434\u0435\u043e \u0444\u0430\u0439\u043b\u043e\u043c \u0438\u043b\u0438 \u043e\u0431\u044b\u0447\u043d\u044b\u043c \u0432\u0438\u0434\u0435\u043e. \u042f \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u044e \u0435\u0433\u043e \u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044e \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442. \u0414\u043b\u044f \u043e\u0442\u043c\u0435\u043d\u044b \u2014 /cancel.")


@dp.message(F.text == "\U0001f4ac \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b")
async def subtitles_button(message: Message):
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
    if message.from_user.id != ADMIN_ID:
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
        raise RuntimeError("FFmpeg \u043d\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d \u043d\u0430 \u0441\u0435\u0440\u0432\u0435\u0440\u0435 Render. \u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438 FFmpeg \u0432 \u043e\u043a\u0440\u0443\u0436\u0435\u043d\u0438\u0438.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u043f\u0440\u0435\u0432\u044b\u0441\u0438\u043b\u0430 \u0434\u043e\u043f\u0443\u0441\u0442\u0438\u043c\u043e\u0435 \u0432\u0440\u0435\u043c\u044f.") from exc
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u0430\u044f \u043e\u0448\u0438\u0431\u043a\u0430 FFmpeg").strip()
        raise RuntimeError(error[-1800:])
    return result.stdout.strip()


def probe_video(path: Path) -> tuple[int, int]:
    output = run_command(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)], timeout=30)
    match = re.match(r"(\d+)x(\d+)", output)
    if not match:
        raise RuntimeError("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u0440\u0430\u0437\u043c\u0435\u0440 \u0432\u0438\u0434\u0435\u043e.")
    return int(match.group(1)), int(match.group(2))


def improve_video(input_path: Path, output_path: Path):
    run_command([
        "ffmpeg", "-hide_banner", "-y", "-i", str(input_path),
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,eq=contrast=1.10:saturation=1.08:brightness=0.01,unsharp=5:5:0.7:3:3:0.0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_path)
    ], timeout=1800)


def transcribe_to_srt(input_path: Path, work_dir: Path) -> Path:
    if WhisperModel is None:
        raise RuntimeError("\u041d\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430 \u0431\u0438\u0431\u043b\u0438\u043e\u0442\u0435\u043a\u0430 faster-whisper. \u0414\u043e\u0431\u0430\u0432\u044c \u0435\u0451 \u0432 requirements.txt.")
    audio_path = work_dir / "audio.wav"
    run_command(["ffmpeg", "-hide_banner", "-y", "-i", str(input_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path)], timeout=600)
    model_name = os.getenv("WHISPER_MODEL", "base")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(audio_path), beam_size=3, language="ru", vad_filter=True)
    srt_path = work_dir / "subtitles.srt"
    def stamp(seconds: float) -> str:
        ms = max(0, int(seconds * 1000))
        hours, ms = divmod(ms, 3_600_000)
        minutes, ms = divmod(ms, 60_000)
        secs, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"
    count = 0
    with srt_path.open("w", encoding="utf-8") as file:
        for seg in segments:
            text = " ".join((seg.text or "").split()).replace("-->", "\u2014>")
            if not text:
                continue
            count += 1
            file.write(f"{count}\n{stamp(seg.start)} --> {stamp(seg.end)}\n{text}\n\n")
    if count == 0:
        raise RuntimeError("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u0442\u044c \u0440\u0435\u0447\u044c. \u0423\u0431\u0435\u0434\u0438\u0441\u044c, \u0447\u0442\u043e \u0432 \u0432\u0438\u0434\u0435\u043e \u0441\u043b\u044b\u0448\u043d\u0430 \u0440\u0435\u0447\u044c.")
    return srt_path


def add_subtitles(input_path: Path, output_path: Path, work_dir: Path):
    width, height = probe_video(input_path)
    srt_path = transcribe_to_srt(input_path, work_dir)
    # Use a generated ASS file so the text is placed around 62% down the frame, below centre.
    ass_path = work_dir / "subtitles.ass"
    play_w, play_h = width, height
    font_size = max(20, min(48, int(height * 0.045)))
    margin_v = max(1, int(height * 0.38))  # from bottom -> baseline around 62% of frame height
    def ass_time(value: str) -> str:
        hh, mm, rest = value.split(":")
        ss, ms = rest.split(",")
        centiseconds = int(ms) // 10
        return f"{int(hh)}:{int(mm):02}:{int(ss):02}.{centiseconds:02}"
    entries = []
    raw = srt_path.read_text(encoding="utf-8-sig").strip()
    for block in re.split(r"\n\s*\n", raw):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, end = [x.strip() for x in lines[1].split("-->", 1)]
        text = r"\N".join(lines[2:]).replace("{", "\\{").replace("}", "\\}")
        entries.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    ass_path.write_text(
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {play_w}\nPlayResY: {play_h}\nScaledBorderAndShadow: yes\n\n"
        f"[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,1,3,0,2,40,40,{margin_v},1\n\n"
        f"[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + "\n".join(entries) + "\n",
        encoding="utf-8"
    )
    escaped = str(ass_path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    run_command([
        "ffmpeg", "-hide_banner", "-y", "-i", str(input_path), "-vf", f"ass='{escaped}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_path)
    ], timeout=2400)


async def process_video(message: Message, action: str):
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
    if get_setting("processing_enabled", "1") != "1" and user_id != ADMIN_ID:
        await message.answer("\u23f8 \u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d\u0430 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u043e\u043c.")
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
        if action == "quality":
            await message.answer("\U0001f6e0 \u0423\u043b\u0443\u0447\u0448\u0430\u044e \u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435 \u0447\u0435\u0440\u0435\u0437 FFmpeg\u2026")
            await asyncio.to_thread(improve_video, source, output)
            caption = "\u2705 \u0413\u043e\u0442\u043e\u0432\u043e! \u0412\u0438\u0434\u0435\u043e \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u043d\u043e: \u0443\u043b\u0443\u0447\u0448\u0435\u043d\u044b \u043a\u043e\u043d\u0442\u0440\u0430\u0441\u0442\u043d\u043e\u0441\u0442\u044c, \u043d\u0430\u0441\u044b\u0449\u0435\u043d\u043d\u043e\u0441\u0442\u044c \u0438 \u0440\u0435\u0437\u043a\u043e\u0441\u0442\u044c."
        elif action == "subtitles":
            await message.answer("\U0001f4dd \u0420\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u044e \u0440\u0435\u0447\u044c \u0438 \u0432\u0441\u0442\u0440\u0430\u0438\u0432\u0430\u044e \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b. \u042d\u0442\u043e \u043c\u043e\u0436\u0435\u0442 \u0437\u0430\u043d\u044f\u0442\u044c \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u043c\u0438\u043d\u0443\u0442\u2026")
            await asyncio.to_thread(add_subtitles, source, output, work_dir)
            caption = "\u2705 \u0413\u043e\u0442\u043e\u0432\u043e! \u0414\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u044b \u0436\u0438\u0440\u043d\u044b\u0435 \u0431\u0435\u043b\u044b\u0435 \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b \u0441 \u0447\u0451\u0440\u043d\u043e\u0439 \u043e\u0431\u0432\u043e\u0434\u043a\u043e\u0439 \u0447\u0443\u0442\u044c \u043d\u0438\u0436\u0435 \u0441\u0435\u0440\u0435\u0434\u0438\u043d\u044b \u043a\u0430\u0434\u0440\u0430."
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
            conn.execute("INSERT INTO tasks(user_id,task_type,status,created_at,updated_at,output_name) VALUES(?,?,'done',?,?,?)", (user_id, action, now_text(), now_text(), output.name))
            conn.commit()
    except Exception as exc:
        log.exception("\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438 \u0432\u0438\u0434\u0435\u043e")
        with db_connect() as conn:
            conn.execute("INSERT INTO tasks(user_id,task_type,status,created_at,updated_at,error) VALUES(?,?,'error',?,?,?)", (user_id, action, now_text(), now_text(), str(exc)[:1000]))
            conn.commit()
        send_admin_alert("video_processing_error", f"\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c: {user_id}\n\u041e\u0448\u0438\u0431\u043a\u0430: {type(exc).__name__}: {exc}")
        reason = str(exc).replace("<", "&lt;").replace(">", "&gt;")[:1200]
        await message.answer(f"\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u0442\u044c \u0432\u0438\u0434\u0435\u043e.\n<code>{reason}</code>\n\n\u0415\u0441\u043b\u0438 \u043e\u0448\u0438\u0431\u043a\u0430 \u043f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0441\u044f, \u043f\u0440\u0438\u0448\u043b\u0438 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0443 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u0441\u0442\u0440\u043e\u043a\u0438 Render Logs.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        set_user_action(user_id, None)


@dp.message(F.video | F.document)
async def media_handler(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    action = get_user_action(user_id)
    if action in {"quality", "subtitles"}:
        await process_video(message, action)
        return
    await message.answer("\u0412\u0438\u0434\u0435\u043e \u043f\u043e\u043b\u0443\u0447\u0438\u043b! \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u044b\u0431\u0435\u0440\u0438 \u0432 \u043c\u0435\u043d\u044e \u00ab\U0001f3ac \u0423\u043b\u0443\u0447\u0448\u0438\u0442\u044c \u043a\u0430\u0447\u0435\u0441\u0442\u0432\u043e\u00bb \u0438\u043b\u0438 \u00ab\U0001f4ac \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b\u00bb, \u0437\u0430\u0442\u0435\u043c \u043e\u0442\u043f\u0440\u0430\u0432\u044c \u0432\u0438\u0434\u0435\u043e \u0435\u0449\u0451 \u0440\u0430\u0437.", reply_markup=main_keyboard())


@dp.message(F.text)
async def fallback_text(message: Message):
    text = (message.text or "").strip().lower()
    if text in {"\u043f\u0440\u0438\u0432\u0435\u0442", "\u043a\u0443", "hello", "hi", "start"}:
        await message.answer(welcome_text(), reply_markup=main_keyboard())
    else:
        await message.answer("\u0412\u044b\u0431\u0435\u0440\u0438 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043a\u043d\u043e\u043f\u043a\u043e\u0439 \u043c\u0435\u043d\u044e \u0438\u043b\u0438 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 /help.", reply_markup=main_keyboard())


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
