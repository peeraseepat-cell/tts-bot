import asyncio
import base64
import contextlib
import io
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union
from zoneinfo import ZoneInfo

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
VOICE_NAME = os.environ.get("TTS_VOICE", "th-TH-Chirp3-HD-Achernar")
LANGUAGE_CODE = VOICE_NAME.rsplit("-Chirp3", 1)[0]
MAX_CHARS = 20000
CHUNK_SIZE = 220
PART_SIZE = int(os.environ.get("TTS_PART_SIZE", 2500))
TTS_PART_MAX_CHUNKS = int(os.environ.get("TTS_PART_MAX_CHUNKS", 4))
COLLECT_WINDOW_SECONDS = float(os.environ.get("COLLECT_WINDOW_SECONDS", 5))
MONTHLY_FREE_CHARS = int(os.environ.get("TTS_MONTHLY_FREE_CHARS", 1_000_000))
USAGE_FILE = Path(os.environ.get("TTS_USAGE_FILE", "usage.json"))
USAGE_TIMEZONE = os.environ.get("TTS_USAGE_TIMEZONE", "Asia/Bangkok")
TTS_MAX_RETRIES = int(os.environ.get("TTS_MAX_RETRIES", 0))
TTS_CONNECT_TIMEOUT = float(os.environ.get("TTS_CONNECT_TIMEOUT", 10))
TTS_READ_TIMEOUT = float(os.environ.get("TTS_READ_TIMEOUT", 15))
TTS_FILE_TIMEOUT = float(os.environ.get("TTS_FILE_TIMEOUT", 30))
PORT = int(os.environ.get("PORT", 8443))

TTS_URL = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"
SENTENCE_ENDINGS = (".", "!", "?", "。", "…")
SOFT_ENDINGS = ",;:，；："


@dataclass
class UsageSummary:
    period: str
    used: int
    remaining: int
    limit: int
    reset_at: datetime
    days_until_reset: int


@dataclass
class PendingChatBuffer:
    texts: list[str] = field(default_factory=list)
    status_message_id: Optional[int] = None
    flush_task: Optional[asyncio.Task] = None


@dataclass
class TTSJob:
    chat_id: int
    status_message_id: int
    text: str
    parts: list[str]
    queued_at: datetime


class UsageMeter:
    def __init__(self, path: Union[str, Path], limit: int, timezone_name: str) -> None:
        self.path = Path(path)
        self.limit = limit
        self.timezone = ZoneInfo(timezone_name)

    def _now(self) -> datetime:
        return datetime.now(self.timezone)

    def _local(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            return now.replace(tzinfo=self.timezone)
        return now.astimezone(self.timezone)

    def _period(self, now: datetime) -> str:
        local_now = self._local(now)
        return local_now.strftime("%Y-%m")

    def _reset_at(self, now: datetime) -> datetime:
        local_now = self._local(now)
        if local_now.month == 12:
            return datetime(local_now.year + 1, 1, 1, tzinfo=self.timezone)
        return datetime(local_now.year, local_now.month + 1, 1, tzinfo=self.timezone)

    def _read(self, now: datetime) -> dict:
        period = self._period(now)
        if not self.path.exists():
            return {"period": period, "used": 0}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"period": period, "used": 0}
        if data.get("period") != period:
            return {"period": period, "used": 0}
        try:
            used = max(int(data.get("used", 0)), 0)
        except (TypeError, ValueError):
            used = 0
        return {"period": period, "used": used}

    def _write(self, data: dict, now: datetime) -> None:
        data = {
            "period": data["period"],
            "used": int(data["used"]),
            "limit": self.limit,
            "updated_at": self._local(now).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def _summary(self, data: dict, now: datetime) -> UsageSummary:
        reset_at = self._reset_at(now)
        seconds_until_reset = max((reset_at - self._local(now)).total_seconds(), 0)
        days_until_reset = math.ceil(seconds_until_reset / 86400)
        used = int(data["used"])
        return UsageSummary(
            period=data["period"],
            used=used,
            remaining=max(self.limit - used, 0),
            limit=self.limit,
            reset_at=reset_at,
            days_until_reset=days_until_reset,
        )

    def record(self, chars: int, now: Optional[datetime] = None) -> UsageSummary:
        current_time = now or self._now()
        data = self._read(current_time)
        data["used"] = int(data["used"]) + max(int(chars), 0)
        self._write(data, current_time)
        return self._summary(data, current_time)

    def preview(self, now: Optional[datetime] = None) -> UsageSummary:
        current_time = now or self._now()
        return self._summary(self._read(current_time), current_time)


USAGE_METER = UsageMeter(USAGE_FILE, MONTHLY_FREE_CHARS, USAGE_TIMEZONE)
PENDING_BUFFERS: dict[int, PendingChatBuffer] = {}
JOB_QUEUE: Optional[asyncio.Queue] = None
WORKER_TASK: Optional[asyncio.Task] = None


def _ensure_sentence_ending(chunk: str) -> str:
    chunk = chunk.strip()
    if not chunk or chunk.endswith(SENTENCE_ENDINGS):
        return chunk
    return chunk.rstrip(SOFT_ENDINGS) + "."


def _split_text(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= CHUNK_SIZE + 1:
        return [_ensure_sentence_ending(text)]
    chunks = []
    while text:
        if len(text) <= CHUNK_SIZE + 1:
            chunks.append(_ensure_sentence_ending(text))
            break
        cut = CHUNK_SIZE
        search_limit = CHUNK_SIZE + 1
        for sep in ["\n", ". ", "! ", "? ", "。", "…", ".", "!", "?", ", ", " "]:
            pos = text.rfind(sep, 0, search_limit)
            if pos > CHUNK_SIZE // 2:
                cut = pos + len(sep)
                break
        chunks.append(_ensure_sentence_ending(text[:cut]))
        text = text[cut:].lstrip()
    return chunks


def _split_output_parts(text: str) -> list[str]:
    chunks = _split_text(text)
    if len(chunks) <= TTS_PART_MAX_CHUNKS and len(text.strip()) <= PART_SIZE:
        return [_ensure_sentence_ending(text)]

    parts = []
    current = []
    current_len = 0
    for chunk in chunks:
        next_len = current_len + len(chunk)
        if current and (len(current) >= TTS_PART_MAX_CHUNKS or next_len > PART_SIZE):
            parts.append(_ensure_sentence_ending("".join(current)))
            current = []
            current_len = 0
        current.append(chunk)
        current_len += len(chunk)
    if current:
        parts.append(_ensure_sentence_ending("".join(current)))
    return parts


def _estimate_tts_requests(parts: list[str]) -> int:
    return sum(len(_split_text(part)) for part in parts)


def _format_usage_summary(summary: UsageSummary, used_this_job: int, requests: int) -> str:
    return (
        f"ใช้รอบนี้ {used_this_job:,} ตัวอักษร / {requests:,} TTS requests\n"
        f"เดือนนี้ใช้แล้ว {summary.used:,}/{summary.limit:,} ตัวอักษร\n"
        f"เหลือ {summary.remaining:,} ตัวอักษร\n"
        f"Reset {summary.reset_at:%Y-%m-%d %H:%M} ({USAGE_TIMEZONE}) อีก {summary.days_until_reset} วัน"
    )


def _get_queue() -> asyncio.Queue:
    global JOB_QUEUE
    if JOB_QUEUE is None:
        JOB_QUEUE = asyncio.Queue()
    return JOB_QUEUE


def _ensure_worker(app: Application) -> None:
    global WORKER_TASK
    if WORKER_TASK is None or WORKER_TASK.done():
        WORKER_TASK = app.create_task(_tts_worker(app))


async def _post_tts_chunk(client: httpx.AsyncClient, chunk: str) -> bytes:
    for attempt in range(TTS_MAX_RETRIES + 1):
        try:
            resp = await client.post(TTS_URL, json={
                "input": {"text": chunk},
                "voice": {"languageCode": LANGUAGE_CODE, "name": VOICE_NAME},
                "audioConfig": {"audioEncoding": "MP3"},
            })
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= TTS_MAX_RETRIES:
                raise RuntimeError(f"Google TTS timed out after {attempt + 1} attempts") from exc
            await asyncio.sleep(2 * (attempt + 1))
            continue

        if resp.status_code == 200:
            return base64.b64decode(resp.json()["audioContent"])

        try:
            err = resp.json().get("error", {}).get("message", resp.text[:200])
        except ValueError:
            err = resp.text[:200]
        if resp.status_code in {429, 500, 502, 503, 504} and attempt < TTS_MAX_RETRIES:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        raise RuntimeError(f"Google TTS: {err}")

    raise RuntimeError("Google TTS failed")


async def _synthesize_part(
    text: str,
    progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> tuple[bytes, int, int, UsageSummary]:
    chunks = _split_text(text)
    timeout = httpx.Timeout(TTS_READ_TIMEOUT, connect=TTS_CONNECT_TIMEOUT)
    audio_parts = []
    used_chars = 0
    summary = USAGE_METER.preview()
    async with httpx.AsyncClient(timeout=timeout) as client:
        for index, chunk in enumerate(chunks, start=1):
            if progress:
                await progress(index, len(chunks))
            audio_parts.append(await _post_tts_chunk(client, chunk))
            used_chars += len(chunk)
            summary = USAGE_METER.record(len(chunk))
    return b"".join(audio_parts), used_chars, len(chunks), summary


async def _synthesize(text: str) -> bytes:
    audio_bytes, _, _, _ = await _synthesize_part(text)
    return audio_bytes


async def _synthesize_part_with_timeout(
    text: str,
    progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> tuple[bytes, int, int, UsageSummary]:
    try:
        return await asyncio.wait_for(
            _synthesize_part(text, progress=progress),
            timeout=TTS_FILE_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"สร้างเสียงไฟล์นี้เกิน {TTS_FILE_TIMEOUT:g} วิ") from exc


async def _safe_edit_or_send(app: Application, chat_id: int, message_id: int, text: str) -> None:
    try:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception:
        with contextlib.suppress(Exception):
            await app.bot.send_message(chat_id=chat_id, text=text)


async def _flush_chat_after_delay(chat_id: int, app: Application) -> None:
    try:
        await asyncio.sleep(COLLECT_WINDOW_SECONDS)
    except asyncio.CancelledError:
        return

    pending = PENDING_BUFFERS.pop(chat_id, None)
    if not pending or not pending.status_message_id:
        return

    text = "\n\n".join(pending.texts).strip()
    if len(text) > MAX_CHARS:
        await _safe_edit_or_send(
            app,
            chat_id,
            pending.status_message_id,
            f"ข้อความรวมยาวเกิน {MAX_CHARS:,} ตัวอักษร ({len(text):,})",
        )
        return

    parts = _split_output_parts(text)
    request_count = _estimate_tts_requests(parts)
    queue = _get_queue()
    queue_position = queue.qsize() + 1
    await _safe_edit_or_send(
        app,
        chat_id,
        pending.status_message_id,
        (
            f"รับข้อความแล้ว {len(text):,} ตัวอักษร\n"
            f"จะแบ่งเป็น {len(parts)} ไฟล์ / ประมาณ {request_count} TTS requests\n"
            f"เข้าคิวลำดับที่ {queue_position}"
        ),
    )
    _ensure_worker(app)
    await queue.put(TTSJob(
        chat_id=chat_id,
        status_message_id=pending.status_message_id,
        text=text,
        parts=parts,
        queued_at=datetime.now(ZoneInfo(USAGE_TIMEZONE)),
    ))


async def _tts_worker(app: Application) -> None:
    queue = _get_queue()
    while True:
        job = await queue.get()
        try:
            await _process_job(app, job)
        finally:
            queue.task_done()


async def _process_job(app: Application, job: TTSJob) -> None:
    total_used = 0
    total_requests = 0
    summary = USAGE_METER.preview()

    for index, part in enumerate(job.parts, start=1):
        await _safe_edit_or_send(
            app,
            job.chat_id,
            job.status_message_id,
            f"กำลังสร้างเสียงไฟล์ {index}/{len(job.parts)}...",
        )
        try:
            async def report_progress(done: int, total: int) -> None:
                await _safe_edit_or_send(
                    app,
                    job.chat_id,
                    job.status_message_id,
                    f"กำลังสร้างเสียงไฟล์ {index}/{len(job.parts)} · ท่อน {done}/{total}...",
                )

            audio_bytes, used_chars, requests, summary = await _synthesize_part_with_timeout(
                part,
                progress=report_progress,
            )
        except Exception as exc:
            await _safe_edit_or_send(
                app,
                job.chat_id,
                job.status_message_id,
                f"เกิดข้อผิดพลาดตอนสร้างไฟล์ {index}/{len(job.parts)}: {exc}",
            )
            return

        total_used += used_chars
        total_requests += requests
        voice = io.BytesIO(audio_bytes)
        voice.name = f"tts_part_{index:02d}_of_{len(job.parts):02d}.mp3"
        await app.bot.send_voice(
            chat_id=job.chat_id,
            voice=voice,
            caption=f"ไฟล์ {index}/{len(job.parts)} · {len(part):,} ตัวอักษร",
        )

    await _safe_edit_or_send(
        app,
        job.chat_id,
        job.status_message_id,
        "เสร็จแล้ว\n" + _format_usage_summary(summary, total_used, total_requests),
    )


async def start(update: Update, context):
    await update.message.reply_text("ส่ง text มา แล้วจะแปลงเป็นเสียงให้ฟัง")


async def handle_text(update: Update, context):
    text = update.message.text.strip()
    if not text:
        return

    chat_id = update.effective_chat.id
    pending = PENDING_BUFFERS.get(chat_id)
    if pending is None:
        msg = await update.message.reply_text(
            f"รับข้อความแล้ว กำลังรอข้อความต่อ {COLLECT_WINDOW_SECONDS:g} วิ..."
        )
        pending = PendingChatBuffer(status_message_id=msg.message_id)
        PENDING_BUFFERS[chat_id] = pending
    elif pending.status_message_id:
        with contextlib.suppress(Exception):
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=pending.status_message_id,
                text=f"รับเพิ่มแล้ว {len(pending.texts) + 1} ข้อความ กำลังรอข้อความต่อ...",
            )

    pending.texts.append(text)
    if pending.flush_task and not pending.flush_task.done():
        pending.flush_task.cancel()
    pending.flush_task = context.application.create_task(
        _flush_chat_after_delay(chat_id, context.application)
    )


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    if webhook_url:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{webhook_url}/webhook",
            url_path="webhook",
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
