# DeadBossBot

Telegram bot project for processing videos with quality enhancement, subtitles, and PRO features.

## What it does
- Main menu in Russian with inline buttons
- Video processing via FFmpeg/FFprobe
- Speech recognition via faster-whisper
- Subtitle generation with FFmpeg/libass
- Telegram Stars billing for PRO (30 days)
- SQLite-based user and payment tracking
- Admin panel for owner only
- Queue, limits, and error handling

## Required environment variables
- `BOT_TOKEN` — Telegram bot token
- `ADMIN_ID` — numeric Telegram ID of the owner/admin
- `PAYMENT_PROVIDER_TOKEN` — Telegram provider token for Stars (optional until enabled)
- `MAX_PARALLEL_TASKS` — optional, default `2`
- `FREE_TASK_LIMIT` — optional, default `2`

## Render setup
1. Connect this repository to Render.
2. In Render Environment Variables add:
   - `BOT_TOKEN`
   - `ADMIN_ID`
   - `PAYMENT_PROVIDER_TOKEN` (if you enable Stars)
3. Deploy as a Background Worker with Docker.
4. Upload a banner file named `banner.mp4` or similar to `assets/`.

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
BOT_TOKEN=... ADMIN_ID=... python main.py
```

## Notes
- The bot requires FFmpeg/FFprobe installed in the container.
- The banner is required only for the video-boost workflow.
- For payment flow, Telegram Stars requires provider token from BotFather / Telegram payment setup.
