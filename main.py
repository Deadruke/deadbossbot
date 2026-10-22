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
    raise RuntimeError("BOT_TOKEN Ð½Ðµ Ð·Ð°Ð´Ð°Ð½ Ð² Environment Variables Render")

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
    text = f"ð¨ <b>DeadBossBot alert</b>\nTime: {now_text()}\nCategory: {category}\n\n{details}"
    async def _send():
        try:
            await bot.send_message(ADMIN_ID, text[:3900])
        except Exception:
            log.exception("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾ÑÐ¿ÑÐ°Ð²Ð¸ÑÑ ÑÐ²ÐµÐ´Ð¾Ð¼Ð»ÐµÐ½Ð¸Ðµ Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÑ")
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send())
    except RuntimeError:
        log.error("Admin alert Ð²Ð½Ðµ event loop: %s", text)


def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="ð¬ Ð£Ð»ÑÑÑÐ¸ÑÑ ÐºÐ°ÑÐµÑÑÐ²Ð¾"), KeyboardButton(text="ð¬ ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ ÑÑÐ±ÑÐ¸ÑÑÑ")],
        [KeyboardButton(text="â­ ÐÑÐ¿Ð¸ÑÑ PRO"), KeyboardButton(text="ð¤ ÐÑÐ¾ÑÐ¸Ð»Ñ")],
        [KeyboardButton(text="ð  ÐÐ´Ð¼Ð¸Ð½")],
    ], resize_keyboard=True)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ð Ð¡ÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ°", callback_data="admin:stats"), InlineKeyboardButton(text="ð ÐÐ±ÑÐ°Ð±Ð¾ÑÐºÐ° Ð²ÐºÐ»/Ð²ÑÐºÐ»", callback_data="admin:toggle_processing")],
        [InlineKeyboardButton(text="ð¥ ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ð¸", callback_data="admin:list_users")],
    ])


def welcome_text():
    return ("ÐÑÐ¸Ð²ÐµÑ! Ð¯ DeadBossBot.\n\n"
            "ð¬ Ð£Ð»ÑÑÑÐ°Ñ Ð¸Ð·Ð¾Ð±ÑÐ°Ð¶ÐµÐ½Ð¸Ðµ Ð²Ð¸Ð´ÐµÐ¾ ÑÐ¸Ð»ÑÑÑÐ°Ð¼Ð¸.\n"
            "ð¬ Ð Ð°ÑÐ¿Ð¾Ð·Ð½Ð°Ñ ÑÑÑÑÐºÑÑ ÑÐµÑÑ Ð¸ Ð´Ð¾Ð±Ð°Ð²Ð»ÑÑ ÑÑÐ±ÑÐ¸ÑÑÑ.\n"
            "â­ PRO â Ð·Ð° Telegram Stars.\n\n"
            "ÐÑÐ±ÐµÑÐ¸ Ð´ÐµÐ¹ÑÑÐ²Ð¸Ðµ Ð² Ð¼ÐµÐ½Ñ, Ð·Ð°ÑÐµÐ¼ Ð¾ÑÐ¿ÑÐ°Ð²Ñ Ð²Ð¸Ð´ÐµÐ¾.")


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
    await message.answer("ÐÐ¾Ð¼Ð°Ð½Ð´Ñ: /start, /help, /profile, /admin, /cancel, /grantpro ID Ð´Ð½Ð¸, /revokepro ID")


async def send_profile(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    with db_connect() as conn:
        row = conn.execute("SELECT stars_balance, pro_until, total_tasks FROM users WHERE user_id=?", (user_id,)).fetchone()
    expiry = int(row["pro_until"] or 0)
    pro_text = f"â PRO Ð´Ð¾ {datetime.fromtimestamp(expiry, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}" if expiry > int(time.time()) else "â PRO Ð½Ðµ Ð°ÐºÑÐ¸Ð²ÐµÐ½"
    await message.answer(f"ð¤ <b>ÐÑÐ¾ÑÐ¸Ð»Ñ</b>\nID: <code>{user_id}</code>\nÐÐ°Ð»Ð°Ð½Ñ: {row['stars_balance']} â­\n{pro_text}\nÐÐ±ÑÐ°Ð±Ð¾ÑÐ°Ð½Ð¾ Ð²Ð¸Ð´ÐµÐ¾: {row['total_tasks']}")


@dp.message(Command("profile"))
async def profile_command(message: Message):
    await send_profile(message)


async def admin_panel(message: Message):
    with db_connect() as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        pros = conn.execute("SELECT COUNT(*) c FROM users WHERE pro_until>?", (int(time.time()),)).fetchone()["c"]
        tasks = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    await message.answer(f"ð  <b>ÐÐ´Ð¼Ð¸Ð½-Ð¿Ð°Ð½ÐµÐ»Ñ</b>\nÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»ÐµÐ¹: {users}\nÐÐºÑÐ¸Ð²Ð½ÑÐ¹ PRO: {pros}\nÐÐ°Ð´Ð°Ñ Ð²ÑÐµÐ³Ð¾: {tasks}\nÐÐ±ÑÐ°Ð±Ð¾ÑÐºÐ°: {'Ð²ÐºÐ»ÑÑÐµÐ½Ð°' if get_setting('processing_enabled','1') == '1' else 'Ð²ÑÐºÐ»ÑÑÐµÐ½Ð°'}", reply_markup=admin_keyboard())


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("ð« ÐÐ¾ÑÑÑÐ¿ ÑÐ¾Ð»ÑÐºÐ¾ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÑ.")
        return
    await admin_panel(message)


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("ÐÐ¾ÑÑÑÐ¿ Ð·Ð°Ð¿ÑÐµÑÑÐ½", show_alert=True)
        return
    if callback.data == "admin:toggle_processing":
        new = "0" if get_setting("processing_enabled", "1") == "1" else "1"
        set_setting("processing_enabled", new)
        if callback.message:
            await callback.message.answer(f"ÐÐ±ÑÐ°Ð±Ð¾ÑÐºÐ° {'Ð²ÐºÐ»ÑÑÐµÐ½Ð°' if new == '1' else 'Ð²ÑÐºÐ»ÑÑÐµÐ½Ð°'}.")
    elif callback.data == "admin:stats":
        if callback.message:
            await admin_panel(callback.message)
    elif callback.data == "admin:list_users":
        with db_connect() as conn:
            rows = conn.execute("SELECT user_id,username,stars_balance,pro_until FROM users ORDER BY user_id DESC LIMIT 10").fetchall()
        text = "ð¥ <b>ÐÐ¾ÑÐ»ÐµÐ´Ð½Ð¸Ðµ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ð¸</b>\n" + "\n".join(f"{r['user_id']} | @{r['username'] or 'â'} | {'PRO' if r['pro_until'] > int(time.time()) else 'â'} | {r['stars_balance']} â­" for r in rows)
        if callback.message:
            await callback.message.answer(text[:3900])
    await callback.answer()


@dp.message(Command("grantpro"))
async def grantpro(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("ð« ÐÐ¾ÑÑÑÐ¿ ÑÐ¾Ð»ÑÐºÐ¾ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÑ.")
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[2].isdigit() or int(parts[2]) < 1:
        await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: /grantpro USER_ID ÐÐÐ")
        return
    target = get_user_by_username_or_id(parts[1])
    if not target and parts[1].isdigit():
        ensure_user(int(parts[1]))
        target = {"user_id": int(parts[1])}
    if not target:
        await message.answer("ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½. ÐÑÑÑÑ ÑÐ½Ð°ÑÐ°Ð»Ð° Ð½Ð°Ð¶Ð¼ÑÑ /start.")
        return
    set_pro_for_user(int(target["user_id"]), int(parts[2]))
    await message.answer(f"â PRO Ð²ÑÐ´Ð°Ð½ Ð½Ð° {parts[2]} Ð´Ð½ÐµÐ¹.")


@dp.message(Command("revokepro"))
async def revokepro(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("ð« ÐÐ¾ÑÑÑÐ¿ ÑÐ¾Ð»ÑÐºÐ¾ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÑ.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Ð¤Ð¾ÑÐ¼Ð°Ñ: /revokepro USER_ID")
        return
    target = get_user_by_username_or_id(parts[1])
    if not target:
        await message.answer("ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        return
    with db_connect() as conn:
        conn.execute("UPDATE users SET pro_until=0 WHERE user_id=?", (target["user_id"],))
        conn.commit()
    await message.answer("â PRO Ð¾ÑÐºÐ»ÑÑÑÐ½.")


@dp.message(Command("cancel"))
async def cancel(message: Message):
    set_user_action(message.from_user.id, None)
    await message.answer("ð ÐÐµÐ¹ÑÑÐ²Ð¸Ðµ Ð¾ÑÐ¼ÐµÐ½ÐµÐ½Ð¾.", reply_markup=main_keyboard())


@dp.message(F.text == "ð¬ Ð£Ð»ÑÑÑÐ¸ÑÑ ÐºÐ°ÑÐµÑÑÐ²Ð¾")
async def quality_button(message: Message):
    set_user_action(message.from_user.id, "quality")
    await message.answer("ð¹ ÐÑÐ¸ÑÐ»Ð¸ Ð²Ð¸Ð´ÐµÐ¾ ÑÐ°Ð¹Ð»Ð¾Ð¼ Ð¸Ð»Ð¸ Ð¾Ð±ÑÑÐ½ÑÐ¼ Ð²Ð¸Ð´ÐµÐ¾. Ð¯ Ð¾Ð±ÑÐ°Ð±Ð¾ÑÐ°Ñ ÐµÐ³Ð¾ Ð¸ Ð¾ÑÐ¿ÑÐ°Ð²Ð»Ñ ÑÐµÐ·ÑÐ»ÑÑÐ°Ñ. ÐÐ»Ñ Ð¾ÑÐ¼ÐµÐ½Ñ â /cancel.")


@dp.message(F.text == "ð¬ ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ ÑÑÐ±ÑÐ¸ÑÑÑ")
async def subtitles_button(message: Message):
    set_user_action(message.from_user.id, "subtitles")
    await message.answer("ð¬ ÐÑÐ¸ÑÐ»Ð¸ Ð²Ð¸Ð´ÐµÐ¾ Ñ ÑÐµÑÑÑ. Ð¯ ÑÐ°ÑÐ¿Ð¾Ð·Ð½Ð°Ñ ÑÑÑÑÐºÑÑ ÑÐµÑÑ Ð¸ Ð²ÑÑÑÐ¾Ñ Ð¶Ð¸ÑÐ½ÑÐµ Ð±ÐµÐ»ÑÐµ ÑÑÐ±ÑÐ¸ÑÑÑ Ñ ÑÑÑÐ½Ð¾Ð¹ Ð¾Ð±Ð²Ð¾Ð´ÐºÐ¾Ð¹ ÑÑÑÑ Ð½Ð¸Ð¶Ðµ ÑÐµÑÐµÐ´Ð¸Ð½Ñ ÐºÐ°Ð´ÑÐ°. ÐÐ»Ñ Ð¾ÑÐ¼ÐµÐ½Ñ â /cancel.")


@dp.message(F.text == "â­ ÐÑÐ¿Ð¸ÑÑ PRO")
async def buy_pro(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    if is_pro_active(user_id):
        await message.answer("â PRO Ñ ÑÐµÐ±Ñ ÑÐ¶Ðµ Ð°ÐºÑÐ¸Ð²ÐµÐ½.")
        return
    payload = f"deadboss:pro:{PRO_DAYS}:{user_id}:{uuid.uuid4().hex}"
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="DeadBossBot PRO",
            description=f"ÐÐ¾Ð´Ð¿Ð¸ÑÐºÐ° PRO Ð½Ð° {PRO_DAYS} Ð´Ð½ÐµÐ¹.",
            payload=payload,
            provider_token="",  # Required for Telegram Stars: empty string, not a provider token.
            currency="XTR",
            prices=[LabeledPrice(label=f"PRO Ð½Ð° {PRO_DAYS} Ð´Ð½ÐµÐ¹", amount=PRO_PRICE_STARS)],
        )
    except Exception as exc:
        log.exception("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ ÑÐ¾Ð·Ð´Ð°ÑÑ ÑÑÑÑ Telegram Stars")
        await message.answer(f"â ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ ÑÐ¾Ð·Ð´Ð°ÑÑ ÑÑÑÑ. ÐÑÐ¾Ð²ÐµÑÑ Ð½Ð°ÑÑÑÐ¾Ð¹ÐºÐ¸ Ð±Ð¾ÑÐ° Ð¸ Ð¿Ð¾Ð¿ÑÐ¾Ð±ÑÐ¹ Ð¿Ð¾Ð·Ð¶Ðµ.\nÐÑÐ¸ÑÐ¸Ð½Ð°: {type(exc).__name__}")


@dp.message(F.text == "ð¤ ÐÑÐ¾ÑÐ¸Ð»Ñ")
async def profile_button(message: Message):
    await send_profile(message)


@dp.message(F.text == "ð  ÐÐ´Ð¼Ð¸Ð½")
async def admin_button(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("ð« ÐÐ¾ÑÑÑÐ¿ ÑÐ¾Ð»ÑÐºÐ¾ Ð²Ð»Ð°Ð´ÐµÐ»ÑÑÑ.")
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
    await bot.answer_pre_checkout_query(query.id, ok=valid, error_message=None if valid else "Ð¡ÑÑÑ Ð½ÐµÐ´ÐµÐ¹ÑÑÐ²Ð¸ÑÐµÐ»ÐµÐ½. ÐÑÐºÑÐ¾Ð¹ Ð¿Ð¾ÐºÑÐ¿ÐºÑ PRO Ð·Ð°Ð½Ð¾Ð²Ð¾.")


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    parts = (payment.invoice_payload or "").split(":")
    if not (len(parts) == 5 and parts[0] == "deadboss" and parts[1] == "pro"
            and parts[3].isdigit() and int(parts[3]) == user_id
            and payment.currency == "XTR" and payment.total_amount == PRO_PRICE_STARS):
        log.error("ÐÐ¾Ð»ÑÑÐµÐ½ Ð¿Ð»Ð°ÑÑÐ¶ Ñ Ð½ÐµÐ¸Ð·Ð²ÐµÑÑÐ½ÑÐ¼ payload Ð¾Ñ user=%s", user_id)
        await message.answer("â ï¸ ÐÐ»Ð°ÑÑÐ¶ Ð¿Ð¾Ð»ÑÑÐµÐ½, Ð½Ð¾ ÑÑÑÑ Ð½Ðµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¿ÑÐ¾Ð²ÐµÑÐ¸ÑÑ Ð°Ð²ÑÐ¾Ð¼Ð°ÑÐ¸ÑÐµÑÐºÐ¸. Ð¡Ð²ÑÐ¶Ð¸ÑÑ Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÐ¾Ð¼.")
        send_admin_alert("payment_validation", f"ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ {user_id}; payload={payment.invoice_payload!r}; currency={payment.currency}; amount={payment.total_amount}")
        return
    charge_id = payment.telegram_payment_charge_id
    if not charge_id:
        await message.answer("â ï¸ ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¿ÑÐ¾Ð²ÐµÑÐ¸ÑÑ Ð¸Ð´ÐµÐ½ÑÐ¸ÑÐ¸ÐºÐ°ÑÐ¾Ñ Ð¿Ð»Ð°ÑÐµÐ¶Ð°. Ð¡Ð²ÑÐ¶Ð¸ÑÑ Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÐ¾Ð¼.")
        return

    # Insert charge and extend PRO in one DB transaction. UNIQUE charge_id prevents duplicate credit.
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT id FROM payments WHERE charge_id=?", (charge_id,)).fetchone()
        if exists:
            conn.rollback()
            await message.answer("â Ð­ÑÐ¾Ñ Ð¿Ð»Ð°ÑÑÐ¶ ÑÐ¶Ðµ ÑÑÑÑÐ½. PRO Ð¿Ð¾Ð²ÑÐ¾ÑÐ½Ð¾ Ð½Ðµ Ð½Ð°ÑÐ¸ÑÐ»ÑÐ»ÑÑ.")
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
        log.exception("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð·Ð°Ð¿Ð¸ÑÐ°ÑÑ Ð¿Ð»Ð°ÑÑÐ¶/PRO")
        send_admin_alert("payment_db_error", f"User {user_id}; charge {charge_id}")
        await message.answer("â ï¸ Telegram Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ´Ð¸Ð» Ð¿Ð»Ð°ÑÑÐ¶, Ð½Ð¾ Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ðµ PRO Ð²ÑÐµÐ¼ÐµÐ½Ð½Ð¾ Ð½Ðµ ÑÐ´Ð°Ð»Ð¾ÑÑ. ÐÐ´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾Ñ ÑÐ²ÐµÐ´Ð¾Ð¼Ð»ÑÐ½; Ð½Ðµ Ð¾Ð¿Ð»Ð°ÑÐ¸Ð²Ð°Ð¹ Ð¿Ð¾Ð²ÑÐ¾ÑÐ½Ð¾.")
        return
    finally:
        conn.close()
    await message.answer(f"ð ÐÐ¿Ð»Ð°ÑÐ° Ð¿Ð¾Ð´ÑÐ²ÐµÑÐ¶Ð´ÐµÐ½Ð°! PRO Ð°ÐºÑÐ¸Ð²Ð¸ÑÐ¾Ð²Ð°Ð½ Ð½Ð° {PRO_DAYS} Ð´Ð½ÐµÐ¹ Ð·Ð° {PRO_PRICE_STARS} â­.")


def run_command(command: list[str], timeout: int | None = None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg Ð½Ðµ ÑÑÑÐ°Ð½Ð¾Ð²Ð»ÐµÐ½ Ð½Ð° ÑÐµÑÐ²ÐµÑÐµ Render. Ð£ÑÑÐ°Ð½Ð¾Ð²Ð¸ FFmpeg Ð² Ð¾ÐºÑÑÐ¶ÐµÐ½Ð¸Ð¸.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ÐÐ±ÑÐ°Ð±Ð¾ÑÐºÐ° Ð¿ÑÐµÐ²ÑÑÐ¸Ð»Ð° Ð´Ð¾Ð¿ÑÑÑÐ¸Ð¼Ð¾Ðµ Ð²ÑÐµÐ¼Ñ.") from exc
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "ÐÐµÐ¸Ð·Ð²ÐµÑÑÐ½Ð°Ñ Ð¾ÑÐ¸Ð±ÐºÐ° FFmpeg").strip()
        raise RuntimeError(error[-1800:])
    return result.stdout.strip()


def probe_video(path: Path) -> tuple[int, int]:
    output = run_command(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)], timeout=30)
    match = re.match(r"(\d+)x(\d+)", output)
    if not match:
        raise RuntimeError("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾Ð¿ÑÐµÐ´ÐµÐ»Ð¸ÑÑ ÑÐ°Ð·Ð¼ÐµÑ Ð²Ð¸Ð´ÐµÐ¾.")
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
        raise RuntimeError("ÐÐµ ÑÑÑÐ°Ð½Ð¾Ð²Ð»ÐµÐ½Ð° Ð±Ð¸Ð±Ð»Ð¸Ð¾ÑÐµÐºÐ° faster-whisper. ÐÐ¾Ð±Ð°Ð²Ñ ÐµÑ Ð² requirements.txt.")
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
            text = " ".join((seg.text or "").split()).replace("-->", "â>")
            if not text:
                continue
            count += 1
            file.write(f"{count}\n{stamp(seg.start)} --> {stamp(seg.end)}\n{text}\n\n")
    if count == 0:
        raise RuntimeError("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ ÑÐ°ÑÐ¿Ð¾Ð·Ð½Ð°ÑÑ ÑÐµÑÑ. Ð£Ð±ÐµÐ´Ð¸ÑÑ, ÑÑÐ¾ Ð² Ð²Ð¸Ð´ÐµÐ¾ ÑÐ»ÑÑÐ½Ð° ÑÐµÑÑ.")
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
        await message.answer("ÐÑÐ¸ÑÐ»Ð¸ Ð²Ð¸Ð´ÐµÐ¾, Ð¿Ð¾Ð¶Ð°Ð»ÑÐ¹ÑÑÐ°.")
        return
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("Ð­ÑÐ¾ Ð½Ðµ Ð¿Ð¾ÑÐ¾Ð¶Ðµ Ð½Ð° Ð²Ð¸Ð´ÐµÐ¾ÑÐ°Ð¹Ð». ÐÑÐ¿ÑÐ°Ð²Ñ Ð²Ð¸Ð´ÐµÐ¾ ÐºÐ°Ðº Ð²Ð¸Ð´ÐµÐ¾ Ð¸Ð»Ð¸ Ð´Ð¾ÐºÑÐ¼ÐµÐ½Ñ Ñ ÑÐ¸Ð¿Ð¾Ð¼ video/.")
        return
    file_size = getattr(file_obj, "file_size", None) or 0
    if file_size and file_size > MAX_VIDEO_MB * 1024 * 1024:
        await message.answer(f"Ð¤Ð°Ð¹Ð» ÑÐ»Ð¸ÑÐºÐ¾Ð¼ Ð±Ð¾Ð»ÑÑÐ¾Ð¹. ÐÐ°ÐºÑÐ¸Ð¼Ð°Ð»ÑÐ½ÑÐ¹ ÑÐ°Ð·Ð¼ÐµÑ Ð´Ð»Ñ ÑÑÐ¾Ð³Ð¾ Ð±Ð¾ÑÐ°: {MAX_VIDEO_MB} ÐÐ.")
        set_user_action(user_id, None)
        return
    if get_setting("processing_enabled", "1") != "1" and user_id != ADMIN_ID:
        await message.answer("â¸ ÐÐ±ÑÐ°Ð±Ð¾ÑÐºÐ° Ð²ÑÐµÐ¼ÐµÐ½Ð½Ð¾ Ð¾ÑÐºÐ»ÑÑÐµÐ½Ð° Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÐ¾Ð¼.")
        return

    work_dir = TMP_DIR / f"job_{user_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        await message.answer("ð¥ Ð¡ÐºÐ°ÑÐ¸Ð²Ð°Ñ Ð²Ð¸Ð´ÐµÐ¾â¦")
        file_info = await bot.get_file(file_obj.file_id)
        original_name = getattr(file_obj, "file_name", None) or "input.mp4"
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
            suffix = ".mp4"
        source = work_dir / f"input{suffix}"
        await bot.download_file(file_info.file_path, destination=source)
        if not source.exists() or source.stat().st_size == 0:
            raise RuntimeError("Telegram Ð½Ðµ ÑÐºÐ°ÑÐ°Ð» Ð²Ð¸Ð´ÐµÐ¾ Ð¸Ð»Ð¸ ÑÐ°Ð¹Ð» Ð¿ÑÑÑÐ¾Ð¹.")
        output = work_dir / "processed.mp4"
        if action == "quality":
            await message.answer("ð  Ð£Ð»ÑÑÑÐ°Ñ Ð¸Ð·Ð¾Ð±ÑÐ°Ð¶ÐµÐ½Ð¸Ðµ ÑÐµÑÐµÐ· FFmpegâ¦")
            await asyncio.to_thread(improve_video, source, output)
            caption = "â ÐÐ¾ÑÐ¾Ð²Ð¾! ÐÐ¸Ð´ÐµÐ¾ Ð¾Ð±ÑÐ°Ð±Ð¾ÑÐ°Ð½Ð¾: ÑÐ»ÑÑÑÐµÐ½Ñ ÐºÐ¾Ð½ÑÑÐ°ÑÑÐ½Ð¾ÑÑÑ, Ð½Ð°ÑÑÑÐµÐ½Ð½Ð¾ÑÑÑ Ð¸ ÑÐµÐ·ÐºÐ¾ÑÑÑ."
        elif action == "subtitles":
            await message.answer("ð Ð Ð°ÑÐ¿Ð¾Ð·Ð½Ð°Ñ ÑÐµÑÑ Ð¸ Ð²ÑÑÑÐ°Ð¸Ð²Ð°Ñ ÑÑÐ±ÑÐ¸ÑÑÑ. Ð­ÑÐ¾ Ð¼Ð¾Ð¶ÐµÑ Ð·Ð°Ð½ÑÑÑ Ð½ÐµÑÐºÐ¾Ð»ÑÐºÐ¾ Ð¼Ð¸Ð½ÑÑâ¦")
            await asyncio.to_thread(add_subtitles, source, output, work_dir)
            caption = "â ÐÐ¾ÑÐ¾Ð²Ð¾! ÐÐ¾Ð±Ð°Ð²Ð»ÐµÐ½Ñ Ð¶Ð¸ÑÐ½ÑÐµ Ð±ÐµÐ»ÑÐµ ÑÑÐ±ÑÐ¸ÑÑÑ Ñ ÑÑÑÐ½Ð¾Ð¹ Ð¾Ð±Ð²Ð¾Ð´ÐºÐ¾Ð¹ ÑÑÑÑ Ð½Ð¸Ð¶Ðµ ÑÐµÑÐµÐ´Ð¸Ð½Ñ ÐºÐ°Ð´ÑÐ°."
        else:
            raise RuntimeError("ÐÐµÐ¸Ð·Ð²ÐµÑÑÐ½ÑÐ¹ ÑÐµÐ¶Ð¸Ð¼ Ð¾Ð±ÑÐ°Ð±Ð¾ÑÐºÐ¸. ÐÐ°Ð¶Ð¼Ð¸ ÐºÐ½Ð¾Ð¿ÐºÑ Ð² Ð¼ÐµÐ½Ñ Ð¸ Ð¾ÑÐ¿ÑÐ°Ð²Ñ Ð²Ð¸Ð´ÐµÐ¾ Ð·Ð°Ð½Ð¾Ð²Ð¾.")
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg Ð½Ðµ ÑÐ¾Ð·Ð´Ð°Ð» Ð³Ð¾ÑÐ¾Ð²ÑÐ¹ Ð²Ð¸Ð´ÐµÐ¾ÑÐ°Ð¹Ð».")
        if output.stat().st_size > 49 * 1024 * 1024:
            raise RuntimeError("ÐÐ¾ÑÐ¾Ð²Ð¾Ðµ Ð²Ð¸Ð´ÐµÐ¾ Ð¿ÑÐµÐ²ÑÑÐ°ÐµÑ Ð»Ð¸Ð¼Ð¸Ñ Ð¾ÑÐ¿ÑÐ°Ð²ÐºÐ¸ Telegram Ð´Ð»Ñ ÑÑÐ¾Ð³Ð¾ Ð±Ð¾ÑÐ°. ÐÐ¾Ð¿ÑÐ¾Ð±ÑÐ¹ Ð±Ð¾Ð»ÐµÐµ ÐºÐ¾ÑÐ¾ÑÐºÐ¾Ðµ Ð²Ð¸Ð´ÐµÐ¾.")
        await message.answer("ð¤ ÐÑÐ¿ÑÐ°Ð²Ð»ÑÑ Ð³Ð¾ÑÐ¾Ð²Ð¾Ðµ Ð²Ð¸Ð´ÐµÐ¾â¦")
        await bot.send_video(message.chat.id, FSInputFile(str(output)), caption=caption, supports_streaming=True)
        with db_connect() as conn:
            conn.execute("UPDATE users SET total_tasks=total_tasks+1 WHERE user_id=?", (user_id,))
            conn.execute("INSERT INTO tasks(user_id,task_type,status,created_at,updated_at,output_name) VALUES(?,?,'done',?,?,?)", (user_id, action, now_text(), now_text(), output.name))
            conn.commit()
    except Exception as exc:
        log.exception("ÐÑÐ¸Ð±ÐºÐ° Ð¾Ð±ÑÐ°Ð±Ð¾ÑÐºÐ¸ Ð²Ð¸Ð´ÐµÐ¾")
        with db_connect() as conn:
            conn.execute("INSERT INTO tasks(user_id,task_type,status,created_at,updated_at,error) VALUES(?,?,'error',?,?,?)", (user_id, action, now_text(), now_text(), str(exc)[:1000]))
            conn.commit()
        send_admin_alert("video_processing_error", f"ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ: {user_id}\nÐÑÐ¸Ð±ÐºÐ°: {type(exc).__name__}: {exc}")
        reason = str(exc).replace("<", "&lt;").replace(">", "&gt;")[:1200]
        await message.answer(f"â ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾Ð±ÑÐ°Ð±Ð¾ÑÐ°ÑÑ Ð²Ð¸Ð´ÐµÐ¾.\n<code>{reason}</code>\n\nÐÑÐ»Ð¸ Ð¾ÑÐ¸Ð±ÐºÐ° Ð¿Ð¾Ð²ÑÐ¾ÑÐ¸ÑÑÑ, Ð¿ÑÐ¸ÑÐ»Ð¸ Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÑ Ð¿Ð¾ÑÐ»ÐµÐ´Ð½Ð¸Ðµ ÑÑÑÐ¾ÐºÐ¸ Render Logs.")
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
    await message.answer("ÐÐ¸Ð´ÐµÐ¾ Ð¿Ð¾Ð»ÑÑÐ¸Ð»! Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð²ÑÐ±ÐµÑÐ¸ Ð² Ð¼ÐµÐ½Ñ Â«ð¬ Ð£Ð»ÑÑÑÐ¸ÑÑ ÐºÐ°ÑÐµÑÑÐ²Ð¾Â» Ð¸Ð»Ð¸ Â«ð¬ ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ ÑÑÐ±ÑÐ¸ÑÑÑÂ», Ð·Ð°ÑÐµÐ¼ Ð¾ÑÐ¿ÑÐ°Ð²Ñ Ð²Ð¸Ð´ÐµÐ¾ ÐµÑÑ ÑÐ°Ð·.", reply_markup=main_keyboard())


@dp.message(F.text)
async def fallback_text(message: Message):
    text = (message.text or "").strip().lower()
    if text in {"Ð¿ÑÐ¸Ð²ÐµÑ", "ÐºÑ", "hello", "hi", "start"}:
        await message.answer(welcome_text(), reply_markup=main_keyboard())
    else:
        await message.answer("ÐÑÐ±ÐµÑÐ¸ Ð´ÐµÐ¹ÑÑÐ²Ð¸Ðµ ÐºÐ½Ð¾Ð¿ÐºÐ¾Ð¹ Ð¼ÐµÐ½Ñ Ð¸Ð»Ð¸ Ð¸ÑÐ¿Ð¾Ð»ÑÐ·ÑÐ¹ /help.", reply_markup=main_keyboard())


async def bot_main():
    ensure_db()
    log.info("Starting DeadBossBot polling")
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False)


def bot_thread_main():
    try:
        asyncio.run(bot_main())
    except Exception as exc:
        log.exception("Telegram polling Ð·Ð°Ð²ÐµÑÑÐ¸Ð»ÑÑ Ñ Ð¾ÑÐ¸Ð±ÐºÐ¾Ð¹")
        # Can't safely send async alerts here after the loop has closed; Render Logs contain traceback.


if __name__ == "__main__":
    ensure_db()
    thread = threading.Thread(target=bot_thread_main, name="telegram-polling", daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
