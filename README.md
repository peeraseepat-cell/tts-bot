# 🗣️ Thai TTS Telegram Bot

> Send text, get natural Thai speech back — powered by Google's Chirp3-HD voices.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-async-26A5E4?logo=telegram&logoColor=white)](https://python-telegram-bot.org/)
[![Google Cloud TTS](https://img.shields.io/badge/Google%20Cloud-Text--to--Speech-4285F4?logo=googlecloud&logoColor=white)](https://cloud.google.com/text-to-speech)
[![Docker ready](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](./Dockerfile)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

A small, production-minded Telegram bot that turns Thai text into high-quality audio. Built for **long text** — paste an article, a chapter, or a wall of messages, and the bot splits it cleanly, narrates it, and streams the audio files back with live progress.

---

## ✨ Features

- **Natural Thai voices** — Google Cloud `Chirp3-HD` (default `th-TH-Chirp3-HD-Achernar`), switchable via env.
- **Handles long text** — up to 20,000 characters per request, auto-split into multiple audio files on sentence boundaries (falls back to soft punctuation, then hard cuts).
- **Smart message batching** — collects rapid-fire messages within a short window and narrates them as one job, so multi-paragraph pastes stay together.
- **Job queue with live progress** — every request is queued; the bot reports its queue position and updates `file x/y · chunk m/n` as it works.
- **Monthly usage metering** — per-user character quota tracking (optional Supabase backend), with a `/status` command and per-job usage summary.
- **Resilient by design** — explicit connect/read/send timeouts, graceful error messages per file, no silent failures.
- **Deploy anywhere** — single `Dockerfile`, configured entirely through environment variables.

## 🤖 Usage

| Command | What it does |
|---------|--------------|
| *(send any text)* | Converts it to Thai speech and returns the audio file(s) |
| `/start` | Quick hint on how to use the bot |
| `/status` | Shows this month's character usage and remaining quota |

Send a long article and you'll see something like:

```
รับข้อความแล้ว 14,230 ตัวอักษร
จะแบ่งเป็น 3 ไฟล์ / ประมาณ 58 TTS requests
เข้าคิวลำดับที่ 1
...
กำลังสร้างเสียงไฟล์ 2/3 · ท่อน 9/20...
เสร็จแล้ว
```

## 🚀 Quick start

### Run locally

```bash
git clone https://github.com/peeraseepat-cell/tts-bot.git
cd tts-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in your keys
python bot.py
```

### Run with Docker

```bash
docker build -t tts-bot .
docker run --env-file .env -p 8443:8443 tts-bot
```

## ⚙️ Configuration

All configuration is via environment variables. Only the first two are required.

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `GOOGLE_API_KEY` | ✅ | — | Google Cloud API key with Text-to-Speech enabled |
| `TTS_VOICE` | | `th-TH-Chirp3-HD-Achernar` | Any Google Cloud TTS voice name |
| `SUPABASE_URL` / `SUPABASE_KEY` | | — | Enables persistent per-user usage metering |
| `TTS_MONTHLY_FREE_CHARS` | | `1000000` | Monthly character quota per user |
| `COLLECT_WINDOW_SECONDS` | | `5` | How long to wait for follow-up messages before narrating |
| `TTS_PART_SIZE` | | `1950` | Target characters per audio file |
| `PORT` | | `8443` | Webhook port |

> 🔒 `GOOGLE_API_KEY` and `SUPABASE_KEY` are read server-side only and never logged. Keep them out of version control — `.env` is gitignored.

## 🧱 How it works

```
Telegram message
      │
      ▼
collect window ──► batch messages into one job
      │
      ▼
split text  ──► sentence-aware chunks ──► grouped into parts (~1950 chars)
      │
      ▼
job queue  ──► Google Cloud TTS (per chunk) ──► concat ──► MP3
      │
      ▼
stream files back  +  update usage meter (Supabase)
```

The core lives in a single, readable `bot.py` (~600 lines). Text splitting and queue/usage logic are covered by unit tests in `tests/`.

## 🧪 Tests

```bash
pip install -r requirements.txt
python -m pytest
```

## 📄 License

[MIT](./LICENSE) — © Peerasee
