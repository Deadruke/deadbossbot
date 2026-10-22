import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import time
import traceback
from datetime import datetime, timedelta, timezone
from multiprocessing import Process
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN", "")
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deadbossbot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
app = Flask(__name__)

ALERT_COOLDOWN_SECONDS = 300
last_admin_alerts = {}


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT,
                stars_balance INTEGER DEFAULT 0,
                pro_until INTEGER DEFAULT 0,
                total_tasks INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                payload TEXT,
                amount_stars INTEGER,
                currency TEXT,
                charge_id TEXT UNIQUE,
                status TEXT,
                created_at TEXT
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
            """
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                action TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('processing_enabled', '1')"
        )
        conn.commit()


def ensure_user(user_id: int, username: str | None, first_name: str | None):
    with db_connect() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, created_at, stars_balance, pro_until, total_tasks)
                VALUES (?, ?, ?, ?, 0, 0, 0)
                """,
                (
                    user_id,
                    username or "",
                    first_name or "",
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


def set_user_action(user_id: int, action: str | None):
    with db_connect() as conn:
        if action is None:
            conn.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
        else:
            conn.execute(
                """
                INSERT INTO user_states (user_id, action, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET action=excluded.action, updated_at=excluded.updated_at
                """,
                (user_id, action, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            )
        conn.commit()


def get_user_action(user_id: int) -> str | None:
    with db_connect() as conn:
        row = conn.execute("SELECT action FROM user_states WHERE user_id = ?", (user_id,)).fetchone()
    return row["action"] if row else None


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


def set_pro_for_user(user_id: int, days: int = 30):
    ensure_user(user_id, None, None)
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
        row = conn.execute(
            "SELECT user_id, username, first_name, stars_balance, pro_until FROM users WHERE user_id = ? OR username = ? LIMIT 1",
            (identifier, identifier),
        ).fetchone()
    return dict(row) if row else None


def get_all_users(limit: int = 20):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id, username, first_name, stars_balance, pro_until FROM users ORDER BY user_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def send_admin_alert(category: str, message: str):
    if ADMIN_ID == 0:
        return

    now = time.time()
    if last_admin_alerts.get(category, 0) and now - last_admin_alerts.get(category, 0) < ALERT_COOLDOWN_SECONDS:
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
            "SELECT username, first_name, stars_balance, pro_until, total_tasks FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    pro_text = "✅ PRO активен" if is_pro_active(user_id) else "❌ PRO не активен"
    if row and row["pro_until"]:
        expiry = datetime.fromtimestamp(int(row["pro_until"]), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        pro_text = f"✅ PRO активен до {expiry}"

    total_tasks = int(row["total_tasks"]) if row else 0
    balance = int(row["stars_balance"]) if row else 0
    await message.answer(
        f"👤 Профиль\n\n"
        f"ID: {user_id}\n"
        f"Username: @{message.from_user.username or '—'}\n"
        f"Баланс: {balance} XTR\n"
        f"{pro_text}\n"
        f"Всего задач: {total_tasks}"
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

    set_pro_for_user(int(found["user_id"]), days=days)
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
        if callback.message:
            await admin_panel(callback.message)
    elif action == "admin:toggle_processing":
        current = get_setting("processing_enabled", "1")
        new = "0" if current == "1" else "1"
        set_setting("processing_enabled", new)
        if callback.message:
            await callback.message.answer(f"🧠 Обработка {'отключена' if new == '0' else 'включена'}.")
    elif action == "admin:clear_errors":
        with db_connect() as conn:
            conn.execute("DELETE FROM tasks WHERE status = 'error'")
            conn.commit()
        if callback.message:
            await callback.message.answer("✅ Ошибки очищены.")
    elif action == "admin:list_users":
        rows = get_all_users(10)
        if not rows:
            if callback.message:
                await callback.message.answer("Пользователи ещё не зарегистрировались.")
            return
        lines = []
        for row in rows:
            pro_status = "PRO" if int(row["pro_until"]) > int(time.time()) else "—"
            lines.append(f"{row['user_id']} | @{row['username'] or '—'} | {pro_status} | {row['stars_balance']} XTR")
        if callback.message:
            await callback.message.answer("👥 Последние пользователи:\n\n" + "\n".join(lines[:10]))
    elif action == "admin:grant_pro":
        if callback.message:
            await callback.message.answer("✅ Выдать PRO можно командой:\n`/grantpro <id|@username> 30`", parse_mode="Markdown")
    elif action == "admin:revoke_pro":
        if callback.message:
            await callback.message.answer("✅ Снять PRO можно командой:\n`/revokepro <id|@username>`", parse_mode="Markdown")

    await callback.answer()


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    set_user_action(message.from_user.id, None)
    await message.answer("🛑 Текущая задача отменена. Отправьте видео снова.")


@dp.message(F.text == "🎬 Улучшить качество")
async def btn_quality(message: Message):
    set_user_action(message.from_user.id, "quality")
    await message.answer(
        "📹 Отправьте видео для улучшения качества.\n\n"
        "После отправки я скачу ролик, обработаю FFmpeg и верну готовый результат."
    )


@dp.message(F.text == "💬 Добавить субтитры")
async def btn_subtitles(message: Message):
    set_user_action(message.from_user.id, "subtitles")
    await message.answer(
        "🎬 Отправьте видео, и я добавлю субтитры.\n\n"
        "Подсказка: обработка может занять несколько минут в зависимости от длительности ролика."
    )


@dp.message(F.text == "⭐ Купить PRO")
async def btn_pro(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    if is_pro_active(user_id):
        await message.answer("✅ У тебя уже активен PRO.")
        return

    if not PAYMENTS_PROVIDER_TOKEN:
        await message.answer(
            "⚠️ Для реальной оплаты Telegram Stars нужно добавить PAYMENTS_PROVIDER_TOKEN в Render Environment Variables."
        )
        return

    payload = f"stars:pro:30:{user_id}"
    await bot.send_invoice(
        chat_id=user_id,
        title="DeadBossBot PRO",
        description="Доступ к PRO на 30 дней. Включает приоритетную обработку, расширенные возможности и доступ к PRO-функциям.",
        payload=payload,
        provider_token=PAYMENTS_PROVIDER_TOKEN,
        currency="XTR",
        prices=[LabeledPrice(label="PRO 30 days", amount=150)],
    )


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


@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    sp = message.successful_payment
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)

    charge_id = sp.provider_payment_charge_id or sp.telegram_payment_charge_id or f"stars:{user_id}:{int(time.time())}"
    with db_connect() as conn:
        exists = conn.execute("SELECT id FROM payments WHERE charge_id = ?", (charge_id,)).fetchone()
        if exists:
            return

        conn.execute(
            """
            INSERT INTO payments (user_id, payload, amount_stars, currency, charge_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'paid', ?)
            """,
            (
                user_id,
                sp.invoice_payload,
                int(sp.total_amount),
                sp.currency,
                charge_id,
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()

    payload = sp.invoice_payload
    if payload.startswith("stars:pro:"):
        try:
            days = int(payload.split(":")[2])
        except (IndexError, ValueError):
            days = 30
        set_pro_for_user(user_id, days)
        await message.answer(
            f"✅ Покупка PRO подтверждена. PRO активен на {days} дней."
        )
    else:
        await message.answer(f"✅ Оплата подтверждена.")


def format_timestamp(ms: int) -> str:
    total_ms = max(0, int(ms))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


async def run_ffmpeg_command(command):
    process = subprocess.run(command, capture_output=True, text=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip() or "FFmpeg failed")
    return True


async def build_subtitle_srt(segments):
    lines = []
    for index, seg in enumerate(segments, start=1):
        start_ms = int(seg.start * 1000)
        end_ms = int(seg.end * 1000)
        text = (seg.text or "").strip().replace("\n", " ")
        if not text:
            continue
        lines.append(str(index))
        lines.append(f"{format_timestamp(start_ms)} --> {format_timestamp(end_ms)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


async def transcribe_audio(audio_path: Path):
    if WhisperModel is None:
        raise RuntimeError("faster-whisper not available in this environment")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio_path), beam_size=5, language="ru")
    seg_list = list(segments)
    if not seg_list:
        raise RuntimeError("Распознавание речи не дало результатов для этого видео.")
    return seg_list


async def improve_video_quality(input_path: Path, output_path: Path):
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,eq=contrast=1.15:saturation=1.2,unsharp=5:5:1.0:5:5:0.0",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    await run_ffmpeg_command(command)


async def add_subtitles_to_video(input_path: Path, output_path: Path):
    audio_path = input_path.parent / f"{input_path.stem}_audio.wav"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(audio_path),
    ]
    await run_ffmpeg_command(command)

    segments = await transcribe_audio(audio_path)
    srt_text = await build_subtitle_srt(segments)
    srt_path = input_path.parent / f"{input_path.stem}_subs.srt"
    srt_path.write_text(srt_text, encoding="utf-8")

    filter_line = (
        "subtitles='{path}':force_style='FontName=DejaVu Sans,Fontsize=26,"
        "PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=3,"
        "Outline=2,Shadow=0,Alignment=5,MarginV=28'"
    ).format(path=str(srt_path).replace("\\", "/"))

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        filter_line,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    await run_ffmpeg_command(command)


async def process_pending_video(message: Message, action: str):
    file = message.video or message.document
    if file is None:
        return

    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("⚠️ Отправьте именно видеофайл.")
        return

    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)

    processed_dir = BASE_DIR / "tmp" / f"video_{user_id}_{int(time.time())}"
    processed_dir.mkdir(parents=True, exist_ok=True)

    try:
        source_name = file.file_name or f"input_{int(time.time())}.mp4"
        source_path = processed_dir / source_name
        await message.answer("📥 Скачивание видео...")
        file_info = await bot.get_file(file.file_id)
        await bot.download_file(file_info.file_path, destination=str(source_path))

        if not source_path.exists():
            raise RuntimeError("Скачивание завершилось без файла.")

        out_name = f"processed_{source_path.stem}.mp4"
        out_path = processed_dir / out_name

        if action == "quality":
            await message.answer("🛠 Улучшение качества видео...")
            await improve_video_quality(source_path, out_path)
            caption = "✅ Готово. Видео улучшено и отправлено ниже."
        elif action == "subtitles":
            await message.answer("📝 Добавляю субтитры...")
            await add_subtitles_to_video(source_path, out_path)
            caption = "✅ Готово. Субтитры добавлены и видео отправлено ниже."
        else:
            caption = "✅ Готово."

        if not out_path.exists():
            raise RuntimeError("Файл после обработки не создан.")

        await message.answer("📤 Отправка результата...")
        await bot.send_video(
            chat_id=user_id,
            video=FSInputFile(str(out_path)),
            caption=caption,
            supports_streaming=True,
        )

        with db_connect() as conn:
            conn.execute(
                "UPDATE users SET total_tasks = total_tasks + 1 WHERE user_id = ?",
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO tasks (user_id, task_type, status, created_at, updated_at, error, output_name)
                VALUES (?, ?, 'done', ?, ?, NULL, ?)
                """,
                (
                    user_id,
                    action,
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    out_name,
                ),
            )
            conn.commit()
    except Exception as exc:
        log.exception("Video processing failed")
        send_admin_alert(
            "video_processing_error",
            f"User: {user_id}\nReason: {type(exc).__name__}: {exc}\n\nTraceback:\n{traceback.format_exc()[:2000]}",
        )
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (user_id, task_type, status, created_at, updated_at, error, output_name)
                VALUES (?, ?, 'error', ?, ?, ?, NULL)
                """,
                (
                    user_id,
                    action,
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    str(exc)[:500],
                ),
            )
            conn.commit()
        await message.answer(f"❌ Ошибка обработки: {exc}")
    finally:
        shutil.rmtree(processed_dir, ignore_errors=True)
        set_user_action(user_id, None)


@dp.message(F.content_type.in_({"video", "document"}))
async def handle_media(message: Message):
    try:
        user_id = message.from_user.id
        action = get_user_action(user_id)

        if action in {"quality", "subtitles"}:
            await process_pending_video(message, action)
            return

        if message.document and not (message.document.mime_type or "").startswith("video/"):
            await message.answer("Отправьте именно видеофайл.")
            return

        file = message.video or message.document
        if file is None:
            return

        ensure_user(user_id, message.from_user.username, message.from_user.first_name)
        await message.answer(
            "✅ Видео получено.\n"
            "Выберите действие в меню: улучшение качества или добавление субтитров.",
            reply_markup=get_main_keyboard(),
        )
    except Exception as exc:
        log.exception("Error while handling media message")
        send_admin_alert(
            "media_error",
            f"User: {message.from_user.id}\nReason: {type(exc).__name__}: {exc}\n\nTraceback:\n{traceback.format_exc()[:2000]}",
        )
        await message.answer("❌ Произошла ошибка при обработке видео. Администратор уже уведомлён.")


async def bot_main():
    try:
        ensure_db()
        log.info("Starting DeadBossBot polling...")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as exc:
        log.exception("Bot polling crashed")
        send_admin_alert(
            "bot_crash",
            f"Reason: {type(exc).__name__}: {exc}\n\nTraceback:\n{traceback.format_exc()[:2000]}",
        )
        raise


def run_bot_process():
    asyncio.run(bot_main())


if __name__ == "__main__":
    try:
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is not set")

        ensure_db()
        process = Process(target=run_bot_process, daemon=True)
        process.start()

        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    except Exception as exc:
        log.exception("Application startup failed")
        try:
            if ADMIN_ID:
                send_admin_alert(
                    "startup_error",
                    f"Reason: {type(exc).__name__}: {exc}\n\nTraceback:\n{traceback.format_exc()[:2000]}",
                )
        except Exception:
            pass
        raise
