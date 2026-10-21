import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
MAX_PARALLEL_TASKS = int(os.getenv("MAX_PARALLEL_TASKS", "2"))
FREE_TASK_LIMIT = int(os.getenv("FREE_TASK_LIMIT", "2"))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "deadboss.db"
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deadboss")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
QUEUE_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_TASKS)
ACTIVE_TASKS = {}
PROCESSING_ENABLED = True


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
                pro_until INTEGER DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                last_task_at TEXT,
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
                invoice_payload TEXT,
                telegram_payment_charge_id TEXT,
                amount INTEGER,
                currency TEXT,
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
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('processing_enabled', '1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('queue_paused', '0')"
        )
        conn.commit()


def get_setting(key, default="0"):
    with db_connect() as conn:
        value = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return (value["value"] if value else default)


def set_setting(key, value):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def ensure_user(user_id: int, username: str | None, first_name: str | None):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
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


def is_pro_active(user_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT pro_until FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return False
    return int(row["pro_until"]) > int(time.time())


def activate_pro(user_id: int, days: int = 30):
    now = int(time.time())
    with db_connect() as conn:
        row = conn.execute(
            "SELECT pro_until FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        current = int(row["pro_until"]) if row and row["pro_until"] else now
        new_expiry = max(current, now) + timedelta(days=days).total_seconds()
        conn.execute(
            "UPDATE users SET pro_until = ? WHERE user_id = ?",
            (int(new_expiry), user_id),
        )
        conn.commit()


def get_user_stats(user_id: int):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT username, first_name, pro_until, total_tasks FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "username": row["username"],
        "first_name": row["first_name"],
        "pro_until": int(row["pro_until"]),
        "total_tasks": int(row["total_tasks"]),
    }


def count_tasks_today(user_id: int) -> int:
    day_start = datetime.utcnow().strftime("%Y-%m-%d 00:00:00")
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE user_id = ? AND created_at >= ?",
            (user_id, day_start),
        ).fetchone()
    return int(row["c"])


def user_free_limit_reached(user_id: int) -> bool:
    if is_pro_active(user_id):
        return False
    return count_tasks_today(user_id) >= FREE_TASK_LIMIT


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


def get_quality_keyboard():
    rows = [
        [
            InlineKeyboardButton(text="360p", callback_data="quality:360"),
            InlineKeyboardButton(text="480p", callback_data="quality:480"),
        ],
        [
            InlineKeyboardButton(text="720p", callback_data="quality:720"),
            InlineKeyboardButton(text="1080p", callback_data="quality:1080"),
        ],
        [
            InlineKeyboardButton(text="Оригинал", callback_data="quality:original"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
                InlineKeyboardButton(text="⏸/▶ Очередь", callback_data="admin:toggle_processing"),
            ],
            [
                InlineKeyboardButton(text="🧹 Очистить логи", callback_data="admin:clear_errors"),
            ],
        ]
    )


async def set_bot_commands():
    commands = [
        ("start", "Старт"),
        ("help", "Помощь"),
        ("profile", "Профиль"),
        ("buy_pro", "PRO"),
        ("admin", "Админ"),
        ("cancel", "Отмена"),
    ]
    await bot.set_my_commands(commands)


async def handle_start(message: Message):
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "Привет! Я DeadBossBot — бот для улучшения видео, добавления субтитров и PRO-ускорения.\n\n"
        "Выбери действие в меню или отправь видео.",
        reply_markup=get_main_keyboard(),
    )


async def handle_help(message: Message):
    await message.answer(
        "📘 Помощь\n\n"
        "- Нажми «🎬 Улучшить качество» и пришли видео\n"
        "- Нажми «💬 Добавить субтитры» и пришли видео\n"
        "- «⭐ Купить PRO» активирует PRO на 30 дней\n"
        "- «👤 Профиль» покажет данные и лимиты\n"
        "- Команда /cancel отменяет текущую задачу\n\n"
        "Если нужна админ-панель — напиши /admin."
    )


async def handle_profile(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    stats = get_user_stats(user_id)
    pro_text = "✅ PRO активен" if is_pro_active(user_id) else "❌ PRO не активен"
    if stats and stats["pro_until"]:
        expiry = datetime.utcfromtimestamp(stats["pro_until"]).strftime("%d.%m.%Y %H:%M")
        pro_text = f"✅ PRO активен до {expiry}"
    limit_info = f"Лимит бесплатных задач: {FREE_TASK_LIMIT} в день"
    await message.answer(
        f"👤 Профиль\n\n"
        f"ID: {user_id}\n"
        f"Username: @{message.from_user.username or '—'}\n"
        f"{pro_text}\n"
        f"{limit_info}\n"
        f"Всего задач: {stats['total_tasks'] if stats else 0}"
    )


async def handle_buy_pro(message: Message):
    if not PAYMENT_PROVIDER_TOKEN:
        await message.answer(
            "⚠️ Оплата Telegram Stars пока отключена. Добавь PAYMENT_PROVIDER_TOKEN и перезапусти бота."
        )
        return
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="DeadBoss PRO",
        description="Подписка PRO на 30 дней. Включает повышенные лимиты и приоритет обработки.",
        payload=f"pro:{message.from_user.id}:{int(time.time())}",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="XTR",
        prices=[LabeledPrice(label="DeadBoss PRO 30 дней", amount=50)],
        max_tip_amount=0,
        suggested_tip_amounts=[],
        protect_content=False,
    )


async def handle_cancel(message: Message):
    user_id = message.from_user.id
    if user_id in ACTIVE_TASKS:
        ACTIVE_TASKS[user_id]["cancelled"] = True
        await message.answer("🛑 Задача помечена как отменённая. Ожидайте завершения текущего шага.")
    else:
        await message.answer("Нет активной задачи для отмены.")


async def handle_pay_support(message: Message):
    await message.answer(
        "📞 Поддержка\n\n"
        "Если платёж не пришёл — напишите @youcoid.\n"
        "Опишите проблему, время оплаты и номер транзакции."
    )


async def check_access_for_task(user_id: int):
    if user_free_limit_reached(user_id):
        return False, "❌ Лимит бесплатных задач исчерпан. Купите PRO или подождите следующий день."
    if get_setting("processing_enabled", "1") == "0":
        return False, "⏸️ Обработка временно отключена администратором."
    return True, "OK"


async def handle_media(message: Message):
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer("Отправьте именно видеофайл.")
        return
    if not message.video and not message.document:
        return

    user_id = message.from_user.id
    pending = message.from_user.id in ACTIVE_TASKS
    if pending:
        await message.answer("У вас уже есть активная задача. Попробуйте позже или используйте /cancel.")
        return

    ok, reason = await check_access_for_task(user_id)
    if not ok:
        await message.answer(reason)
        return

    mode = message.from_user.id and message.from_user.id in ACTIVE_TASKS
    if not mode:
        # default to enhance if the user is sending a video directly
        await ask_for_quality(message)


async def ask_for_quality(message: Message):
    await message.answer(
        "🎬 Выбери качество обработки:",
        reply_markup=get_quality_keyboard(),
    )


async def handle_main_menu_text(message: Message):
    text = message.text or ""
    if text == "🎬 Улучшить качество":
        await ask_for_quality(message)
        return
    if text == "💬 Добавить субтитры":
        await message.answer("📹 Пришлите видео, и я добавлю субтитры автоматически.")
        return
    if text == "⭐ Купить PRO":
        await handle_buy_pro(message)
        return
    if text == "👤 Профиль":
        await handle_profile(message)
        return


async def handle_quality_choice(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    quality = callback.data.split(":", 1)[1]
    if callback.message is None:
        return

    ok, reason = await check_access_for_task(user_id)
    if not ok:
        await callback.message.answer(reason)
        return

    media = callback.message.reply_to_message
    if media is None:
        await callback.message.answer("Сначала пришлите видео, потом выберите качество.")
        return
    if media.video is None and media.document is None:
        await callback.message.answer("Видео не найдено в сообщении. Пришлите заново.")
        return

    file_id = media.video.file_id if media.video else media.document.file_id
    await process_video_file(callback.message, file_id, quality)


async def process_video_file(message: Message, file_id: str, quality: str):
    user_id = message.from_user.id
    if user_id in ACTIVE_TASKS:
        await message.answer("У вас уже есть активная задача.")
        return

    with db_connect() as conn:
        conn.execute(
            "INSERT INTO tasks (user_id, task_type, status, created_at, updated_at) VALUES (?, 'enhance', 'queued', ?, ?)",
            (user_id, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()

    status = await message.answer("⏳ Начинаю обработку видео...")
    ACTIVE_TASKS[user_id] = {"cancelled": False, "status_message": status}

    async with QUEUE_SEMAPHORE:
        try:
            await status.edit_text("⬇️ Скачиваю видео...")
            temp_dir = tempfile.mkdtemp(prefix="deadboss_")
            source = Path(temp_dir) / "input.mp4"
            output = Path(temp_dir) / "processed.mp4"

            file = await bot.get_file(file_id)
            await file.download(destination=str(source))

            await status.edit_text("🔍 Проверяю параметры видео...")
            info = await asyncio.to_thread(probe_video, source)
            if info["has_audio"]:
                await status.edit_text("🎚 Улучшаю качество и звук...")
            else:
                await status.edit_text("🎚 Улучшаю качество видео...")

            command = build_enhance_command(source, output, quality, info)
            await asyncio.to_thread(run_subprocess, command)

            if ACTIVE_TASKS.get(user_id, {}).get("cancelled"):
                await status.edit_text("🛑 Задача отменена.")
                return

            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError("Итоговый файл не создался.")

            await status.edit_text("📤 Отправляю файл...")
            await message.answer_video(
                video=output.read_bytes(),
                caption="✅ Готово! Видео улучшено.",
            )

            with db_connect() as conn:
                conn.execute(
                    "UPDATE users SET total_tasks = total_tasks + 1 WHERE user_id = ?",
                    (user_id,),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'done', updated_at = ? WHERE user_id = ? AND status = 'queued' ORDER BY id DESC LIMIT 1",
                    (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), user_id),
                )
                conn.commit()
        except Exception as exc:
            log.exception("Video processing failed")
            await status.edit_text(f"❌ Ошибка: {str(exc)[:2500]}")
            with db_connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'error', error = ?, updated_at = ? WHERE user_id = ? AND status = 'queued' ORDER BY id DESC LIMIT 1",
                    (str(exc)[:1000], datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), user_id),
                )
                conn.commit()
        finally:
            ACTIVE_TASKS.pop(user_id, None)
            shutil.rmtree(temp_dir, ignore_errors=True)


async def handle_subtitles(message: Message):
    user_id = message.from_user.id
    if user_id in ACTIVE_TASKS:
        await message.answer("У вас уже есть активная задача.")
        return

    ok, reason = await check_access_for_task(user_id)
    if not ok:
        await message.answer(reason)
        return

    if message.video is None and message.document is None:
        await message.answer("Отправьте видео, к которому надо добавить субтитры.")
        return

    file_id = message.video.file_id if message.video else message.document.file_id
    await process_subtitles_file(message, file_id)


async def process_subtitles_file(message: Message, file_id: str):
    user_id = message.from_user.id
    status = await message.answer("⏳ Подготовка субтитров...")
    ACTIVE_TASKS[user_id] = {"cancelled": False, "status_message": status}

    async with QUEUE_SEMAPHORE:
        try:
            temp_dir = tempfile.mkdtemp(prefix="deadboss_subs_")
            source = Path(temp_dir) / "input.mp4"
            wav = Path(temp_dir) / "audio.wav"
            srt = Path(temp_dir) / "subtitles.srt"
            output = Path(temp_dir) / "subtitles_video.mp4"

            file = await bot.get_file(file_id)
            await file.download(destination=str(source))

            await status.edit_text("🎙 Извлекаю аудио и распознаю речь...")
            extract_audio_command = [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(wav),
            ]
            await asyncio.to_thread(run_subprocess, extract_audio_command)
            await asyncio.to_thread(make_srt_from_wav, wav, srt)

            if ACTIVE_TASKS.get(user_id, {}).get("cancelled"):
                await status.edit_text("🛑 Задача отменена.")
                return

            await status.edit_text("🎬 Добавляю субтитры в видео...")
            vf = (
                "subtitles='" + str(srt).replace("\\", "/") + "':"
                "force_style='FontName=DejaVu Sans,FontSize=28,Bold=1,"
                "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                "BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=45'"
            )
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output),
            ]
            await asyncio.to_thread(run_subprocess, cmd)

            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError("Ошибка генерации субтитров.")

            await status.edit_text("📤 Отправляю результат...")
            await message.answer_video(
                video=output.read_bytes(),
                caption="✅ Субтитры добавлены.",
            )

            with db_connect() as conn:
                conn.execute(
                    "UPDATE users SET total_tasks = total_tasks + 1 WHERE user_id = ?",
                    (user_id,),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'done', updated_at = ? WHERE user_id = ? AND status = 'queued' ORDER BY id DESC LIMIT 1",
                    (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), user_id),
                )
                conn.commit()
        except Exception as exc:
            log.exception("Subtitle generation failed")
            await status.edit_text(f"❌ Ошибка: {str(exc)[:2500]}")
            with db_connect() as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'error', error = ?, updated_at = ? WHERE user_id = ? AND status = 'queued' ORDER BY id DESC LIMIT 1",
                    (str(exc)[:1000], datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), user_id),
                )
                conn.commit()
        finally:
            ACTIVE_TASKS.pop(user_id, None)
            shutil.rmtree(temp_dir, ignore_errors=True)


def run_subprocess(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:2000] or "FFmpeg error")
    return result


def build_enhance_command(source: Path, output: Path, quality: str, info: dict):
    width, height = info["width"], info["height"]
    if quality == "original":
        out_w, out_h = width, height
    else:
        out_h = int(quality)
        out_w = int(round(width * out_h / height))
    out_w -= out_w % 2
    out_h -= out_h % 2

    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,"
        "unsharp=5:5:0.8:3:3:0.4,"
        "eq=contrast=1.03:saturation=1.05:brightness=0.02,"
        "hqdn3d=2:1.5:3:2.5"
    )
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        vf,
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    return command


def probe_video(path: Path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:2000] or "ffprobe failed")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("Файл не содержит видео.")
    duration = float(data["format"].get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Не удалось определить длительность файла.")
    return {
        "duration": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def make_srt_from_wav(wav_path: Path, srt_path: Path):
    from faster_whisper import WhisperModel

    model = WhisperModel(os.getenv("WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(wav_path), beam_size=1, vad_filter=True)

    def stamp(seconds):
        ms = int(round(seconds * 1000))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    with open(srt_path, "w", encoding="utf-8") as f:
        idx = 0
        for segment in segments:
            text = (segment.text or "").strip()
            if not text:
                continue
            idx += 1
            start = max(0.0, float(segment.start))
            end = max(start + 0.1, float(segment.end))
            f.write(f"{idx}\n{stamp(start)} --> {stamp(end)}\n{text}\n\n")

    return idx


async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 Доступ только владельцу.")
        return
    with db_connect() as conn:
        user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        pro_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE pro_until > ?", (int(time.time()),)).fetchone()["c"]
        task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        error_count = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = 'error'").fetchone()["c"]
        payment_count = conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"]
        processing_enabled = get_setting("processing_enabled", "1")
    text = (
        "📊 Админ-панель\n\n"
        f"Пользователи: {user_count}\n"
        f"PRO: {pro_count}\n"
        f"Задания: {task_count}\n"
        f"Ошибки: {error_count}\n"
        f"Платежи: {payment_count}\n"
        f"Обработка: {'включена' if processing_enabled == '1' else 'отключена'}\n"
        f"Очередь: {len(ACTIVE_TASKS)}"
    )
    await message.answer(text, reply_markup=build_admin_keyboard())


async def handle_admin_action(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("🚫 Доступ запрещён.")
        return

    action = callback.data or ""
    if action == "admin:stats":
        await admin_stats(callback.message)
    elif action == "admin:toggle_processing":
        current = get_setting("processing_enabled", "1")
        new = "0" if current == "1" else "1"
        set_setting("processing_enabled", new)
        await callback.message.answer(f"🧠 Обработка {'отключена' if new == '0' else 'включена'}.")
    elif action == "admin:clear_errors":
        with db_connect() as conn:
            conn.execute("DELETE FROM tasks WHERE status = 'error'")
            conn.commit()
        await callback.message.answer("✅ Ошибки удалены.")
    await callback.answer()


async def handle_pre_checkout_query(query):
    await bot.answer_pre_checkout_query(query.id, ok=True)


async def handle_successful_payment(message: Message):
    payment = message.successful_payment
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    payload = payment.invoice_payload
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, invoice_payload, telegram_payment_charge_id, amount, currency, status, created_at, payload) VALUES (?, ?, ?, ?, ?, 'paid', ?, ?)",
            (
                user_id,
                payload,
                payment.telegram_payment_charge_id,
                payment.total_amount,
                payment.currency,
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                payload,
            ),
        )
        conn.commit()
    activate_pro(user_id, days=30)
    await message.answer("✅ PRO активирован на 30 дней. Спасибо!")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await handle_start(message)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await handle_help(message)


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    await handle_profile(message)


@dp.message(Command("buy_pro"))
async def cmd_buy_pro(message: Message):
    await handle_buy_pro(message)


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await handle_pay_support(message)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    await handle_cancel(message)


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    await admin_stats(message)


@dp.message(F.text == "🎬 Улучшить качество")
async def btn_enhance(message: Message):
    await ask_for_quality(message)


@dp.message(F.text == "💬 Добавить субтитры")
async def btn_subtitles(message: Message):
    await message.answer("📹 Пришлите видео, и я добавлю субтитры автоматически.")


@dp.message(F.text == "⭐ Купить PRO")
async def btn_buy_pro(message: Message):
    await handle_buy_pro(message)


@dp.message(F.text == "👤 Профиль")
async def btn_profile(message: Message):
    await handle_profile(message)


@dp.callback_query(F.data.startswith("quality:"))
async def callback_quality(callback: CallbackQuery):
    await handle_quality_choice(callback)


@dp.callback_query(F.data.startswith("admin:"))
async def callback_admin(callback: CallbackQuery):
    await handle_admin_action(callback)


@dp.pre_checkout_query()
async def pre_checkout(query):
    await handle_pre_checkout_query(query)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    await handle_successful_payment(message)


@dp.message(F.content_type.in_({"video", "document"}))
async def on_video(message: Message):
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        return
    if message.from_user.id == ADMIN_ID and message.text is not None:
        return
    # For submenu process
    if message.from_user.id in ACTIVE_TASKS:
        return
    if message.caption and "subtitles" in (message.caption.lower()):
        await process_subtitles_file(message, message.video.file_id if message.video else message.document.file_id)
        return
    await ask_for_quality(message)


async def main():
    ensure_db()
    await set_bot_commands()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
