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
    raise RuntimeError("BOT_TOKEN не задан в Environment Variables Render")

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
    text = f"🚨 <b>DeadBossBot alert</b>\nTime: {now_text()}\nCategory: {category}\n\n{details}"
    async def _send():
        try:
            await bot.send_message(ADMIN_ID, text[:3900])
        except Exception:
            log.exception("Не удалось отправить уведомление администратору")
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send())
    except RuntimeError:
        log.error("Admin alert вне event loop: %s", text)


def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎬 Улучшить качество"), KeyboardButton(text="💬 Добавить субтитры")],
        [KeyboardButton(text="⭐ Купить PRO"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🛠 Админ")],
    ], resize_keyboard=True)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="🔄 Обработка вкл/выкл", callback_data="admin:toggle_processing")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:list_users")],
    ])


def welcome_text():
    return ("Привет! Я DeadBossBot.\n\n"
            "🎬 Улучшаю изображение видео фильтрами.\n"
            "💬 Распознаю русскую речь и добавляю субтитры.\n"
            "⭐ PRO — за Telegram Stars.\n\n"
            "Выбери действие в меню, затем отправь видео.")


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
    await message.answer("Команды: /start, /help, /profile, /admin, /cancel, /grantpro ID дни, /revokepro ID")


async def send_profile(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    with db_connect() as conn:
        row = conn.execute("SELECT stars_balance, pro_until, total_tasks FROM users WHERE user_id=?", (user_id,)).fetchone()
    expiry = int(row["pro_until"] or 0)
    pro_text = f"✅ PRO до {datetime.fromtimestamp(expiry, timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}" if expiry > int(time.time()) else "❌ PRO не активен"
    await message.answer(f"👤 <b>Профиль</b>\nID: <code>{user_id}</code>\nБаланс: {row['stars_balance']} ⭐\n{pro_text}\nОбработано видео: {row['total_tasks']}")


@dp.message(Command("profile"))
async def profile_command(message: Message):
    await send_profile(message)


async def admin_panel(message: Message):
    with db_connect() as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        pros = conn.execute("SELECT COUNT(*) c FROM users WHERE pro_until>?", (int(time.time()),)).fetchone()["c"]
        tasks = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    await message.answer(f"🛠 <b>Админ-панель</b>\nПользователей: {users}\nАктивный PRO: {pros}\nЗадач всего: {tasks}\nОбработка: {'включена' if get_setting('processing_enabled','1') == '1' else 'выключена'}", reply_markup=admin_keyboard())


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
        return
    await admin_panel(message)


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    if callback.data == "admin:toggle_processing":
        new = "0" if get_setting("processing_enabled", "1") == "1" else "1"
        set_setting("processing_enabled", new)
        if callback.message:
            await callback.message.answer(f"Обработка {'включена' if new == '1' else 'выключена'}.")
    elif callback.data == "admin:stats":
        if callback.message:
            await admin_panel(callback.message)
    elif callback.data == "admin:list_users":
        with db_connect() as conn:
            rows = conn.execute("SELECT user_id,username,stars_balance,pro_until FROM users ORDER BY user_id DESC LIMIT 10").fetchall()
        text = "👥 <b>Последние пользователи</b>\n" + "\n".join(f"{r['user_id']} | @{r['username'] or '—'} | {'PRO' if r['pro_until'] > int(time.time()) else '—'} | {r['stars_balance']} ⭐" for r in rows)
        if callback.message:
            await callback.message.answer(text[:3900])
    await callback.answer()


@dp.message(Command("grantpro"))
async def grantpro(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[2].isdigit() or int(parts[2]) < 1:
        await message.answer("Формат: /grantpro USER_ID ДНИ")
        return
    target = get_user_by_username_or_id(parts[1])
    if not target and parts[1].isdigit():
        ensure_user(int(parts[1]))
        target = {"user_id": int(parts[1])}
    if not target:
        await message.answer("Пользователь не найден. Пусть сначала нажмёт /start.")
        return
    set_pro_for_user(int(target["user_id"]), int(parts[2]))
    await message.answer(f"✅ PRO выдан на {parts[2]} дней.")


@dp.message(Command("revokepro"))
async def revokepro(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /revokepro USER_ID")
        return
    target = get_user_by_username_or_id(parts[1])
    if not target:
        await message.answer("Пользователь не найден.")
        return
    with db_connect() as conn:
        conn.execute("UPDATE users SET pro_until=0 WHERE user_id=?", (target["user_id"],))
        conn.commit()
    await message.answer("✅ PRO отключён.")


@dp.message(Command("cancel"))
async def cancel(message: Message):
    set_user_action(message.from_user.id, None)
    await message.answer("🛑 Действие отменено.", reply_markup=main_keyboard())


@dp.message(F.text == "🎬 Улучшить качество")
async def quality_button(message: Message):
    set_user_action(message.from_user.id, "quality")
    await message.answer("📹 Пришли видео файлом или обычным видео. Я обработаю его и отправлю результат. Для отмены — /cancel.")


@dp.message(F.text == "💬 Добавить субтитры")
async def subtitles_button(message: Message):
    set_user_action(message.from_user.id, "subtitles")
    await message.answer("🎬 Пришли видео с речью. Я распознаю русскую речь и встрою жирные белые субтитры с чёрной обводкой чуть ниже середины кадра. Для отмены — /cancel.")


@dp.message(F.text == "⭐ Купить PRO")
async def buy_pro(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    if is_pro_active(user_id):
        await message.answer("✅ PRO у тебя уже активен.")
        return
    payload = f"deadboss:pro:{PRO_DAYS}:{user_id}:{uuid.uuid4().hex}"
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title="DeadBossBot PRO",
            description=f"Подписка PRO на {PRO_DAYS} дней.",
            payload=payload,
            provider_token="",  # Required for Telegram Stars: empty string, not a provider token.
            currency="XTR",
            prices=[LabeledPrice(label=f"PRO на {PRO_DAYS} дней", amount=PRO_PRICE_STARS)],
        )
    except Exception as exc:
        log.exception("Не удалось создать счёт Telegram Stars")
        await message.answer(f"❌ Не удалось создать счёт. Проверь настройки бота и попробуй позже.\nПричина: {type(exc).__name__}")


@dp.message(F.text == "👤 Профиль")
async def profile_button(message: Message):
    await send_profile(message)


@dp.message(F.text == "🛠 Админ")
async def admin_button(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
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
    await bot.answer_pre_checkout_query(query.id, ok=valid, error_message=None if valid else "Счёт недействителен. Открой покупку PRO заново.")


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    parts = (payment.invoice_payload or "").split(":")
    if not (len(parts) == 5 and parts[0] == "deadboss" and parts[1] == "pro"
            and parts[3].isdigit() and int(parts[3]) == user_id
            and payment.currency == "XTR" and payment.total_amount == PRO_PRICE_STARS):
        log.error("Получен платёж с неизвестным payload от user=%s", user_id)
        await message.answer("⚠️ Платёж получен, но счёт не удалось проверить автоматически. Свяжись с администратором.")
        send_admin_alert("payment_validation", f"Пользователь {user_id}; payload={payment.invoice_payload!r}; currency={payment.currency}; amount={payment.total_amount}")
        return
    charge_id = payment.telegram_payment_charge_id
    if not charge_id:
        await message.answer("⚠️ Не удалось проверить идентификатор платежа. Свяжись с администратором.")
        return

    # Insert charge and extend PRO in one DB transaction. UNIQUE charge_id prevents duplicate credit.
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT id FROM payments WHERE charge_id=?", (charge_id,)).fetchone()
        if exists:
            conn.rollback()
            await message.answer("✅ Этот платёж уже учтён. PRO повторно не начислялся.")
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
        log.exception("Не удалось записать платёж/PRO")
        send_admin_alert("payment_db_error", f"User {user_id}; charge {charge_id}")
        await message.answer("⚠️ Telegram подтвердил платёж, но обновление PRO временно не удалось. Администратор уведомлён; не оплачивай повторно.")
        return
    finally:
        conn.close()
    await message.answer(f"🎉 Оплата подтверждена! PRO активирован на {PRO_DAYS} дней за {PRO_PRICE_STARS} ⭐.")


def run_command(command: list[str], timeout: int | None = None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg не установлен на сервере Render. Установи FFmpeg в окружении.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Обработка превысила допустимое время.") from exc
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


def improve_video(input_path: Path, output_path: Path):
    run_command([
        "ffmpeg", "-hide_banner", "-y", "-i", str(input_path),
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,eq=contrast=1.10:saturation=1.08:brightness=0.01,unsharp=5:5:0.7:3:3:0.0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_path)
    ], timeout=1800)


def transcribe_to_srt(input_path: Path, work_dir: Path) -> Path:
    if WhisperModel is None:
        raise RuntimeError("Не установлена библиотека faster-whisper. Добавь её в requirements.txt.")
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
            text = " ".join((seg.text or "").split()).replace("-->", "—>")
            if not text:
                continue
            count += 1
            file.write(f"{count}\n{stamp(seg.start)} --> {stamp(seg.end)}\n{text}\n\n")
    if count == 0:
        raise RuntimeError("Не удалось распознать речь. Убедись, что в видео слышна речь.")
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
        await message.answer("Пришли видео, пожалуйста.")
        return
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("Это не похоже на видеофайл. Отправь видео как видео или документ с типом video/.")
        return
    file_size = getattr(file_obj, "file_size", None) or 0
    if file_size and file_size > MAX_VIDEO_MB * 1024 * 1024:
        await message.answer(f"Файл слишком большой. Максимальный размер для этого бота: {MAX_VIDEO_MB} МБ.")
        set_user_action(user_id, None)
        return
    if get_setting("processing_enabled", "1") != "1" and user_id != ADMIN_ID:
        await message.answer("⏸ Обработка временно отключена администратором.")
        return

    work_dir = TMP_DIR / f"job_{user_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        await message.answer("📥 Скачиваю видео…")
        file_info = await bot.get_file(file_obj.file_id)
        original_name = getattr(file_obj, "file_name", None) or "input.mp4"
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
            suffix = ".mp4"
        source = work_dir / f"input{suffix}"
        await bot.download_file(file_info.file_path, destination=source)
        if not source.exists() or source.stat().st_size == 0:
            raise RuntimeError("Telegram не скачал видео или файл пустой.")
        output = work_dir / "processed.mp4"
        if action == "quality":
            await message.answer("🛠 Улучшаю изображение через FFmpeg…")
            await asyncio.to_thread(improve_video, source, output)
            caption = "✅ Готово! Видео обработано: улучшены контрастность, насыщенность и резкость."
        elif action == "subtitles":
            await message.answer("📝 Распознаю речь и встраиваю субтитры. Это может занять несколько минут…")
            await asyncio.to_thread(add_subtitles, source, output, work_dir)
            caption = "✅ Готово! Добавлены жирные белые субтитры с чёрной обводкой чуть ниже середины кадра."
        else:
            raise RuntimeError("Неизвестный режим обработки. Нажми кнопку в меню и отправь видео заново.")
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg не создал готовый видеофайл.")
        if output.stat().st_size > 49 * 1024 * 1024:
            raise RuntimeError("Готовое видео превышает лимит отправки Telegram для этого бота. Попробуй более короткое видео.")
        await message.answer("📤 Отправляю готовое видео…")
        await bot.send_video(message.chat.id, FSInputFile(str(output)), caption=caption, supports_streaming=True)
        with db_connect() as conn:
            conn.execute("UPDATE users SET total_tasks=total_tasks+1 WHERE user_id=?", (user_id,))
            conn.execute("INSERT INTO tasks(user_id,task_type,status,created_at,updated_at,output_name) VALUES(?,?,'done',?,?,?)", (user_id, action, now_text(), now_text(), output.name))
            conn.commit()
    except Exception as exc:
        log.exception("Ошибка обработки видео")
        with db_connect() as conn:
            conn.execute("INSERT INTO tasks(user_id,task_type,status,created_at,updated_at,error) VALUES(?,?,'error',?,?,?)", (user_id, action, now_text(), now_text(), str(exc)[:1000]))
            conn.commit()
        send_admin_alert("video_processing_error", f"Пользователь: {user_id}\nОшибка: {type(exc).__name__}: {exc}")
        reason = str(exc).replace("<", "&lt;").replace(">", "&gt;")[:1200]
        await message.answer(f"❌ Не удалось обработать видео.\n<code>{reason}</code>\n\nЕсли ошибка повторится, пришли администратору последние строки Render Logs.")
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
    await message.answer("Видео получил! Сначала выбери в меню «🎬 Улучшить качество» или «💬 Добавить субтитры», затем отправь видео ещё раз.", reply_markup=main_keyboard())


@dp.message(F.text)
async def fallback_text(message: Message):
    text = (message.text or "").strip().lower()
    if text in {"привет", "ку", "hello", "hi", "start"}:
        await message.answer(welcome_text(), reply_markup=main_keyboard())
    else:
        await message.answer("Выбери действие кнопкой меню или используй /help.", reply_markup=main_keyboard())


async def bot_main():
    ensure_db()
    log.info("Starting DeadBossBot polling")
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


def bot_thread_main():
    try:
        asyncio.run(bot_main())
    except Exception as exc:
        log.exception("Telegram polling завершился с ошибкой")
        # Can't safely send async alerts here after the loop has closed; Render Logs contain traceback.


if __name__ == "__main__":
    ensure_db()
    thread = threading.Thread(target=bot_thread_main, name="telegram-polling", daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
