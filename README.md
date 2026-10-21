import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
BASE_DIR = Path(__file__).resolve().parent
ASSETS = BASE_DIR / "assets"

QUALITIES = ["original", "360", "480", "720", "1080"]
MAX_SIZE_MB = 1800
MAX_CONCURRENT = 1

# Настройки баннера
BANNER_SPEED = 1.4
BANNER_FIRST = 30
BANNER_EVERY = 60

# Настройки кодирования
PRESET = "veryfast"
CRF = "23"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("video-bot")

whisper_model = None
active_jobs = {}
job_slots = asyncio.Semaphore(MAX_CONCURRENT)


def find_banner():
    """Ищет banner-файл в папке assets."""
    if not ASSETS.is_dir():
        return None
    for p in sorted(ASSETS.iterdir()):
        if p.is_file() and p.name.lower().startswith("banner"):
            return p
    return None


def get_model():
    global whisper_model
    if whisper_model is None:
        from faster_whisper import WhisperModel

        log.info("[SUBS] Загружаем модель распознавания...")
        whisper_model = WhisperModel(
            os.getenv("WHISPER_MODEL", "base"),
            device="cpu",
            compute_type="int8",
        )
        log.info("[SUBS] Модель загружена")
    return whisper_model


def run_checked(command, label):
    log.info("[%s] Запуск", label)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        error = result.stderr[-2500:]
        log.error("[%s] Ошибка: %s", label, error)
        raise RuntimeError(error or f"{label}: ошибка FFmpeg")
    log.info("[%s] Завершено", label)
    return result


def probe_video(path):
    result = run_checked(
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
        "PROBE",
    )

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("В файле не найдено видео")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(data["format"].get("duration") or 0)
    size = int(data["format"].get("size", 0))

    if duration <= 0:
        raise RuntimeError("Не удалось определить длительность видео")

    return {
        "duration": duration,
        "size": size,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": has_audio,
    }


def load_audio(source):
    import numpy as np

    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "f32le",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="ignore")[-1500:])

    audio = np.frombuffer(result.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("Не удалось достать звук из видео")
    return audio


def make_srt(source, srt_path):
    log.info("[SUBS] Начинаем распознавание речи")
    audio = load_audio(source)
    model = get_model()
    segments, info = model.transcribe(
        audio,
        beam_size=1,
        vad_filter=True,
    )

    def stamp(seconds):
        ms = int(seconds * 1000)
        hours, ms = divmod(ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        secs, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"

    count = 0
    with open(srt_path, "w", encoding="utf-8") as f:
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            start = max(0, segment.start)
            end = max(start + 0.1, segment.end)
            count += 1
            f.write(
                f"{count}\n"
                f"{stamp(start)} --> {stamp(end)}\n"
                f"{text}\n\n"
            )

    log.info("[SUBS] Готово: %s фрагментов; язык: %s", count, info.language)
    return count


def target_size(info, quality):
    width, height = info["width"], info["height"]
    if quality == "original":
        out_w, out_h = width, height
    else:
        out_h = int(quality)
        out_w = int(round(width * out_h / height))
    out_w -= out_w % 2
    out_h -= out_h % 2
    return max(out_w, 2), max(out_h, 2)


def prepare_banner(banner, out_path, out_w, out_h, period):
    banner_info = probe_video(banner)
    banner_len = banner_info["duration"] / BANNER_SPEED
    pad = max(period - banner_len, 0) + 1

    graph = (
        f"[0:v]setpts=PTS/{BANNER_SPEED},"
        f"scale={out_w}:{out_h}:flags=bicubic,fps=30,format=yuv420p,"
        f"tpad=stop_mode=add:stop_duration={pad:.3f}:color=0x00FF00[v]"
    )
    maps = ["-map", "[v]"]
    if banner_info["has_audio"]:
        graph += f";[0:a]atempo={BANNER_SPEED},apad[a]"
        maps += ["-map", "[a]"]

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(banner),
        "-filter_complex",
        graph,
        *maps,
        "-t",
        str(period),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    ]
    if banner_info["has_audio"]:
        command += ["-c:a", "aac", "-b:a", "128k"]
    command.append(str(out_path))

    run_checked(command, "BANNER")
    return banner_info["has_audio"], banner_len


def build_command(source, output, srt, quality, info, banner_prepped,
                  banner_has_audio, first_start, subs_count):
    out_w, out_h = target_size(info, quality)
    duration = info["duration"]

    video_chain = (
        "hqdn3d=2:1.5:3:2.5,"
        f"scale={out_w}:{out_h}:flags=lanczos,"
        "unsharp=5:5:0.8:3:3:0.4,"
        "eq=contrast=1.06:saturation=1.12:brightness=0.01"
    )

    if subs_count > 0:
        subtitle_path = str(Path(srt).resolve()).replace("\\", "/")
        subtitle_path = subtitle_path.replace(":", r"\:")
        video_chain += (
            f",subtitles='{subtitle_path}':"
            "force_style='FontName=Liberation Sans,"
            "FontSize=24,Bold=1,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,"
            "BorderStyle=1,Outline=3,Shadow=1,"
            "Alignment=2,MarginV=45'"
        )

    command = ["ffmpeg", "-y", "-i", str(source)]
    parts = []
    audio_map = ["-map", "0:a:0?"]

    if banner_prepped:
        command += ["-stream_loop", "-1", "-i", str(banner_prepped)]
        parts.append(f"[0:v]{video_chain},format=yuv420p[base]")
        parts.append(
            f"[1:v]setpts=PTS+{first_start:.3f}/TB,"
            "chromakey=0x00FF00:0.20:0.10,format=yuva420p[ad]"
        )
        parts.append(
            "[base][ad]overlay=0:0:eof_action=pass:"
            f"enable='gte(t,{first_start:.3f})'[v]"
        )
        if banner_has_audio:
            delay_ms = int(first_start * 1000)
            if info["has_audio"]:
                parts.append(f"[1:a]adelay={delay_ms}:all=1[ba]")
                parts.append(
                    "[0:a][ba]amix=inputs=2:duration=first:"
                    "dropout_transition=0,volume=2,alimiter=limit=0.95[a]"
                )
            else:
                parts.append(f"[1:a]adelay={delay_ms}:all=1,apad[a]")
            audio_map = ["-map", "[a]"]
    else:
        parts.append(f"[0:v]{video_chain},format=yuv420p[v]")

    command += [
        "-filter_complex",
        ";".join(parts),
        "-map",
        "[v]",
        *audio_map,
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        PRESET,
        "-crf",
        CRF,
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output),
    ]
    return command


async def encode_video(command, output, duration, progress_message, user_id):
    log_path = output.parent / "ffmpeg.log"
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    active_jobs[user_id]["process"] = proc
    last_percent = 0

    async def read_stderr():
        with open(log_path, "wb") as f:
            while True:
                chunk = await proc.stderr.read(8192)
                if not chunk:
                    break
                f.write(chunk)

    stderr_task = asyncio.create_task(read_stderr())

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            text = line.decode("utf-8", errors="ignore").strip()
            if text.startswith("out_time_ms="):
                try:
                    elapsed = int(text.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue

                percent = max(0, min(99, int(elapsed / duration * 100)))
                now = time.monotonic()
                job = active_jobs.get(user_id)
                if (
                    job
                    and percent > last_percent
                    and now - job["last_edit"] > 3
                ):
                    last_percent = percent
                    job["last_edit"] = now
                    try:
                        await progress_message.edit_text(
                            f"🎬 Обрабатываю видео: {percent}%"
                        )
                    except Exception:
                        pass

        await proc.wait()
        await stderr_task

        if proc.returncode != 0:
            error = log_path.read_text(encoding="utf-8", errors="ignore")[-2500:]
            raise RuntimeError(error or "Ошибка FFmpeg")

        log.info("[ENCODE] Завершено")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not stderr_task.done():
            stderr_task.cancel()
        if user_id in active_jobs:
            active_jobs[user_id]["process"] = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Видеобот\n\n"
        "Отправь видео как видео или файл.\n"
        "Я улучшу качество, распознаю речь, добавлю субтитры и баннер,\n"
        "и отправлю результат файлом.\n\n"
        "/cancel — отменить текущую задачу\n"
        "/last — повторно отправить последний результат"
    )


async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    video = msg.video or msg.document

    if not video:
        return

    if msg.document and not (msg.document.mime_type or "").startswith("video/"):
        await msg.reply_text("Отправь видео, пожалуйста.")
        return

    size = getattr(video, "file_size", None) or 0
    if size > MAX_SIZE_MB * 1024 * 1024:
        await msg.reply_text(f"Файл слишком большой. Лимит бота: {MAX_SIZE_MB} МБ.")
        return

    context.user_data["file_id"] = video.file_id
    context.user_data["file_name"] = getattr(video, "file_name", None) or "video.mp4"

    keyboard = [
        [
            InlineKeyboardButton("Оригинал", callback_data="q:original"),
            InlineKeyboardButton("360p", callback_data="q:360"),
            InlineKeyboardButton("480p", callback_data="q:480"),
        ],
        [
            InlineKeyboardButton("720p", callback_data="q:720"),
            InlineKeyboardButton("1080p", callback_data="q:1080"),
        ],
    ]

    await msg.reply_text(
        "Выбери разрешение. Качество улучшу, субтитры и баннер добавлю:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        do_quote=True,
    )


async def choose_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id in active_jobs:
        await query.message.reply_text("У тебя уже есть активная задача. Используй /cancel.")
        return

    file_id = context.user_data.get("file_id")
    if not file_id:
        original = query.message.reply_to_message
        original_video = (original.video or original.document) if original else None
        if original_video:
            file_id = original_video.file_id
    if not file_id:
        await query.message.reply_text("Не вижу видео. Отправь видео ещё раз.")
        return

    quality = query.data.split(":", 1)[1]
    if quality not in QUALITIES:
        await query.message.reply_text("Неизвестное качество.")
        return

    status = await query.message.reply_text("⏳ Подготавливаю задачу...")

    active_jobs[user_id] = {
        "process": None,
        "cancel": False,
        "last_edit": 0,
        "status": status,
    }

    try:
        async with job_slots:
            if active_jobs[user_id]["cancel"]:
                return

            with tempfile.TemporaryDirectory(prefix="video_bot_") as tmp:
                workdir = Path(tmp)
                source = workdir / "input.mp4"
                output = workdir / "processed.mp4"
                srt = workdir / "subtitles.srt"
                banner_prepped = workdir / "banner_prepped.mp4"

                await status.edit_text("⬇️ Скачиваю видео...")
                telegram_file = await context.bot.get_file(file_id)
                await telegram_file.download_to_drive(custom_path=str(source))
                log.info("[BOT] Скачано %.2f МБ", source.stat().st_size / 1048576)

                if active_jobs[user_id]["cancel"]:
                    return

                await status.edit_text("🔍 Проверяю параметры видео...")
                info = await asyncio.to_thread(probe_video, source)

                subs_count = 0
                if info["has_audio"]:
                    await status.edit_text("📝 Распознаю речь и создаю субтитры...")
                    subs_count = await asyncio.to_thread(make_srt, source, srt)
                else:
                    log.info("[SUBS] В видео нет звука, субтитры пропущены")

                if active_jobs[user_id]["cancel"]:
                    return

                banner = find_banner()
                if banner is None:
                    raise RuntimeError(
                        "Не найден файл баннера. Положи видео-баннер в папку assets "
                        "и назови его так, чтобы имя начиналось со слова banner."
                    )

                await status.edit_text("🎞 Готовлю баннер...")
                out_w, out_h = target_size(info, quality)
                period = BANNER_EVERY
                banner_has_audio, banner_len = await asyncio.to_thread(
                    prepare_banner, banner, banner_prepped, out_w, out_h, period
                )

                duration = info["duration"]
                if duration >= BANNER_FIRST + banner_len + 1:
                    first_start = BANNER_FIRST
                else:
                    first_start = max(0.0, (duration - banner_len) / 2)

                command = build_command(
                    source,
                    output,
                    srt,
                    quality,
                    info,
                    banner_prepped,
                    banner_has_audio,
                    first_start,
                    subs_count,
                )

                await status.edit_text("🎬 Обрабатываю видео: 0%")
                await encode_video(command, output, duration, status, user_id)

                if active_jobs[user_id]["cancel"]:
                    return

                if not output.exists() or output.stat().st_size == 0:
                    raise RuntimeError("Итоговый файл не создан")

                await status.edit_text("📤 Отправляю готовый файл...")
                with output.open("rb") as f:
                    sent = await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=f,
                        filename="processed_video.mp4",
                        caption="✅ Готово!",
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

                context.user_data["last_result"] = sent.document.file_id
                await status.edit_text("✅ Готово! Видео отправлено файлом.")

    except Exception as e:
        log.exception("[BOT] Обработка не удалась")
        try:
            await status.edit_text(
                "❌ Не получилось обработать видео.\n\n" + str(e)[-2500:]
            )
        except Exception:
            pass
    finally:
        active_jobs.pop(user_id, None)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    job = active_jobs.get(user_id)

    if not job:
        await update.message.reply_text("Активной обработки нет.")
        return

    job["cancel"] = True
    proc = job.get("process")
    if proc and proc.returncode is None:
        proc.kill()

    await update.message.reply_text("🛑 Отмена запрошена. Ожидай завершения текущего шага.")


async def last_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = context.user_data.get("last_result")
    if not file_id:
        await update.message.reply_text("Готового результата пока нет в текущей сессии.")
        return

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=file_id,
        caption="📁 Последний готовый файл",
    )


async def post_init(app):
    for program in ("ffmpeg", "ffprobe"):
        if not shutil.which(program):
            raise RuntimeError(f"{program} не установлен на Render")
    log.info("[BOT] FFmpeg и ffprobe найдены")
    log.info("[BOT] Баннер: %s", find_banner())


def main():
    if not TOKEN:
        raise RuntimeError("Добавь BOT_TOKEN в Environment на Render")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("last", last_result))
    app.add_handler(CallbackQueryHandler(choose_quality, pattern=r"^q:"))
    app.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video)
    )

    log.info("[BOT] Запуск")
    app.run_polling()


if __name__ == "__main__":
    main()
