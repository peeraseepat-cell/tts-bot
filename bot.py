import asyncio
import base64
import contextlib
import hashlib
import io
import logging
import math
import os
import random
import re
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import httpx
import lameenc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

try:
    from supabase import create_client
except ImportError:
    create_client = None

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
VOICE_NAME = os.environ.get("TTS_VOICE", "th-TH-Chirp3-HD-Achernar")
LANGUAGE_CODE = VOICE_NAME.rsplit("-Chirp3", 1)[0]
MAX_CHARS = 20000
CHUNK_SIZE = 250
PART_SIZE = int(os.environ.get("TTS_PART_SIZE", 1950))
TTS_PART_MAX_CHUNKS = int(os.environ.get("TTS_PART_MAX_CHUNKS", 12))
COLLECT_WINDOW_SECONDS = float(os.environ.get("COLLECT_WINDOW_SECONDS", 5))
MONTHLY_FREE_CHARS = int(os.environ.get("TTS_MONTHLY_FREE_CHARS", 1_000_000))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
USAGE_TIMEZONE = os.environ.get("TTS_USAGE_TIMEZONE", "Asia/Bangkok")
TTS_MAX_RETRIES = int(os.environ.get("TTS_MAX_RETRIES", 0))
TTS_CONNECT_TIMEOUT = float(os.environ.get("TTS_CONNECT_TIMEOUT", 10))
TTS_READ_TIMEOUT = float(os.environ.get("TTS_READ_TIMEOUT", 15))
TTS_FILE_TIMEOUT = float(os.environ.get("TTS_FILE_TIMEOUT", 60))
TELEGRAM_SEND_TIMEOUT = float(os.environ.get("TELEGRAM_SEND_TIMEOUT", 90))
PORT = int(os.environ.get("PORT", 8443))

# Gemini TTS — used whenever GEMINI_API_KEY is set; Chirp stays as the fallback.
# Free tier: 3 RPM · 10K input tokens/min (counted ~2× promptTokenCount) · 10 RPD.
# A 14,000-char Thai part ≈ 4,600 prompt tokens ≈ 9.2K counted — one part per minute fits.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
GEMINI_PART_SIZE = int(os.environ.get("GEMINI_PART_SIZE", 14000))
GEMINI_MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", 60))
GEMINI_READ_TIMEOUT = float(os.environ.get("GEMINI_READ_TIMEOUT", 180))
GEMINI_MP3_KBPS = int(os.environ.get("GEMINI_MP3_KBPS", 48))
GEMINI_MIN_SPLIT = 500
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _parse_allowed_chat_ids(raw: str) -> frozenset[int]:
    return frozenset(int(part) for part in raw.replace(",", " ").split())


ALLOWED_CHAT_IDS = _parse_allowed_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class Persona:
    key: str
    name: str
    voice: str
    style: str


# Voice + style pairs chosen by Boommer in AI Studio Playground (2026-09-26). The style goes in
# the request's speechMetadata.style, exactly as Playground "Get code" sends it.
BROADCAST_STYLE = "Style: Professional, authoritative, clear articulation with standard broadcast cadence."
PERSONAS = [
    Persona("jan", "น้องแจน", "Sami", BROADCAST_STYLE),
    Persona("tom", "คุณทอม", "Achird", BROADCAST_STYLE),
    Persona("leng", "ป้าเล้ง", "Kore", BROADCAST_STYLE),
    Persona("dak", "พี่แด๊ก", "Algenib", BROADCAST_STYLE),
]
PERSONA_BY_KEY = {p.key: p for p in PERSONAS}
RANDOM_PERSONA = "random"
DEFAULT_PERSONA = os.environ.get("TTS_PERSONA", "jan") if os.environ.get("TTS_PERSONA", "jan") in PERSONA_BY_KEY else "jan"
PERSONA_TAG_RE = re.compile(r"#persona=(\w+)")


@dataclass
class TTSJob:
    chat_id: int
    status_message_id: int
    text: str
    parts: list[str]
    queued_at: datetime
    engine: str = "chirp"
    persona: str = DEFAULT_PERSONA


class GeminiQuotaError(Exception):
    """429 from Gemini. kind: "tokens" (input tokens/min — the part is too big, halve it),
    "minute" (requests/min or unknown — wait retry_delay, retry once), "day" (fall back to Chirp)."""

    def __init__(self, kind: str, retry_delay: Optional[float], message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_delay = retry_delay


class GeminiPacer:
    """Keeps at least min_interval seconds between Gemini requests (covers RPM and input TPM)."""

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: Optional[float] = None

    async def wait(self) -> None:
        if self._last is not None:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                await self._sleep(remaining)
        self._last = self._clock()


class UsageMeter:
    def __init__(
        self,
        limit: int,
        timezone_name: str,
        client: Optional[Any] = None,
        table_name: str = "tts_usage",
    ) -> None:
        self.limit = limit
        self.timezone = ZoneInfo(timezone_name)
        self.table_name = table_name
        self.client = client if client is not None else self._create_supabase_client()
        self._fallback_used_by_period: dict[str, int] = {}
        self._warned: set[str] = set()

    def _now(self) -> datetime:
        return datetime.now(self.timezone)

    def _create_supabase_client(self) -> Optional[Any]:
        if create_client is None:
            LOGGER.warning("Supabase usage disabled: supabase package is not installed")
            return None
        if not SUPABASE_URL or not SUPABASE_KEY:
            LOGGER.warning("Supabase usage disabled: SUPABASE_URL or SUPABASE_KEY is not configured")
            return None
        try:
            return create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as exc:
            LOGGER.warning("Supabase usage disabled: failed to initialize client: %s", exc)
            return None

    def _warn_once(self, key: str, message: str, exc: Exception) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        LOGGER.warning("%s: %s", message, exc)

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
        if self.client is None:
            return {"period": period, "used": self._fallback_used_by_period.get(period, 0)}
        try:
            response = (
                self.client.table(self.table_name)
                .select("period,used,limit,updated_at")
                .eq("period", period)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            self._warn_once("read", "Supabase usage read failed; using in-memory fallback", exc)
            return {"period": period, "used": self._fallback_used_by_period.get(period, 0)}

        rows = response.data or []
        if not rows:
            return {"period": period, "used": self._fallback_used_by_period.get(period, 0)}
        try:
            used = max(int(rows[0].get("used", 0)), 0)
        except (TypeError, ValueError):
            used = 0
        return {"period": period, "used": used}

    def _write(self, data: dict, now: datetime) -> bool:
        period = data["period"]
        self._fallback_used_by_period[period] = int(data["used"])
        if self.client is None:
            return False
        row = {
            "period": data["period"],
            "used": int(data["used"]),
            "limit": self.limit,
            "updated_at": self._local(now).isoformat(),
        }
        try:
            self.client.table(self.table_name).upsert(row, on_conflict="period").execute()
        except Exception as exc:
            self._warn_once("write", "Supabase usage write failed; using in-memory fallback", exc)
            return False
        return True

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


USAGE_METER = UsageMeter(MONTHLY_FREE_CHARS, USAGE_TIMEZONE)
PENDING_BUFFERS: dict[int, PendingChatBuffer] = {}
JOB_QUEUE: Optional[asyncio.Queue] = None
WORKER_TASK: Optional[asyncio.Task] = None
GEMINI_PACER = GeminiPacer(GEMINI_MIN_INTERVAL)
CHAT_PERSONA: dict[int, str] = {}         # chat → saved choice (a persona key or "random")
SETTINGS_MESSAGE_ID: dict[int, int] = {}  # chat → the bot's pinned settings message


def _resolve_persona(key: str, choice: Optional[Callable[[list[str]], str]] = None) -> str:
    if key == RANDOM_PERSONA:
        return (choice or random.choice)([p.key for p in PERSONAS])
    return key if key in PERSONA_BY_KEY else DEFAULT_PERSONA


def _persona_label(key: str) -> str:
    if key == RANDOM_PERSONA:
        return "🎲 สุ่ม"
    p = PERSONA_BY_KEY[key]
    return f"{p.name} ({p.voice})"


def _persona_tag(key: str) -> str:
    return f"⚙️ ผู้อ่าน: {_persona_label(key)}\n#persona={key}"


def _parse_pinned_persona(message: Any, bot_id: int) -> Optional[str]:
    """The saved choice lives in a message the bot pinned. Trust only the bot's own tagged pin."""
    text = getattr(message, "text", None)
    author = getattr(message, "from_user", None)
    if not text or author is None or author.id != bot_id:
        return None
    match = PERSONA_TAG_RE.search(text)
    key = match.group(1) if match else None
    return key if key in PERSONA_BY_KEY or key == RANDOM_PERSONA else None


def _settings_keyboard(current: str) -> InlineKeyboardMarkup:
    keys = [p.key for p in PERSONAS] + [RANDOM_PERSONA]
    buttons = [
        InlineKeyboardButton(("✓ " if key == current else "") + _persona_label(key), callback_data=f"persona:{key}")
        for key in keys
    ]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


async def _get_chat_persona(app: Application, chat_id: int) -> str:
    """Saved choice for this chat. Survives restarts through the pinned settings message
    (no database): read once per process, then cached. Any failure → the default."""
    if chat_id in CHAT_PERSONA:
        return CHAT_PERSONA[chat_id]
    key = DEFAULT_PERSONA
    try:
        chat = await app.bot.get_chat(chat_id)
        pinned = getattr(chat, "pinned_message", None)
        parsed = _parse_pinned_persona(pinned, app.bot.id)
        if parsed:
            key = parsed
            SETTINGS_MESSAGE_ID[chat_id] = pinned.message_id
    except Exception as exc:
        LOGGER.warning("Could not read pinned persona for chat %s: %s", chat_id, exc)
    CHAT_PERSONA[chat_id] = key
    return key


async def _save_chat_persona(app: Application, chat_id: int, key: str) -> None:
    CHAT_PERSONA[chat_id] = key
    text = _persona_tag(key)
    message_id = SETTINGS_MESSAGE_ID.get(chat_id)
    if message_id is not None:
        try:
            await app.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
            LOGGER.warning("Pinned persona edit failed (%s) — pinning a new one", exc)
    try:
        sent = await app.bot.send_message(chat_id=chat_id, text=text)
        await app.bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
        SETTINGS_MESSAGE_ID[chat_id] = sent.message_id
    except Exception as exc:
        LOGGER.warning("Persona for chat %s not persisted (kept in memory only): %s", chat_id, exc)


def _ensure_sentence_ending(chunk: str) -> str:
    chunk = chunk.strip()
    if not chunk or chunk.endswith(SENTENCE_ENDINGS):
        return chunk
    return chunk.rstrip(SOFT_ENDINGS) + "."


def _split_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    text = text.strip()
    if len(text) <= size + 1:
        return [_ensure_sentence_ending(text)]
    chunks = []
    while text:
        if len(text) <= size + 1:
            chunks.append(_ensure_sentence_ending(text))
            break
        cut = size
        search_limit = size + 1
        for sep in ["\n", ". ", "! ", "? ", "。", "…", ".", "!", "?", ", ", " "]:
            pos = text.rfind(sep, 0, search_limit)
            if pos > size // 2:
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


def _split_gemini_parts(text: str) -> list[str]:
    return _split_text(text, GEMINI_PART_SIZE)


def _engine() -> str:
    return "gemini" if (GEMINI_API_KEY or "").strip() else "chirp"


def _estimate_tts_requests(parts: list[str]) -> int:
    return sum(len(_split_text(part)) for part in parts)


def _format_usage_summary(summary: UsageSummary, used_this_job: int, requests: int) -> str:
    return (
        f"ใช้รอบนี้ {used_this_job:,} ตัวอักษร / {requests:,} TTS requests\n"
        f"เดือนนี้ใช้แล้ว {summary.used:,}/{summary.limit:,} ตัวอักษร\n"
        f"เหลือ {summary.remaining:,} ตัวอักษร\n"
        f"Reset {summary.reset_at:%Y-%m-%d %H:%M} ({USAGE_TIMEZONE}) อีก {summary.days_until_reset} วัน"
    )


def _format_runtime_status(summary: UsageSummary) -> str:
    return (
        "TTS bot status\n"
        f"engine: {_engine()}" + (f" ({GEMINI_MODEL})" if _engine() == "gemini" else "") + "\n"
        f"collect window: {COLLECT_WINDOW_SECONDS:g}s\n"
        f"part max chunks: {TTS_PART_MAX_CHUNKS}\n"
        f"TTS read timeout: {TTS_READ_TIMEOUT:g}s\n"
        f"TTS file timeout: {TTS_FILE_TIMEOUT:g}s\n"
        f"Telegram send timeout: {TELEGRAM_SEND_TIMEOUT:g}s\n"
        f"usage: {summary.used:,}/{summary.limit:,} chars\n"
        f"reset: {summary.reset_at:%Y-%m-%d %H:%M} ({USAGE_TIMEZONE})"
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
    chunk_hash = hashlib.sha1(chunk.encode()).hexdigest()[:8]
    chunk_bytes = len(chunk.encode("utf-8"))
    for attempt in range(TTS_MAX_RETRIES + 1):
        start = time.monotonic()
        try:
            resp = await client.post(TTS_URL, json={
                "input": {"text": chunk},
                "voice": {"languageCode": LANGUAGE_CODE, "name": VOICE_NAME},
                "audioConfig": {"audioEncoding": "MP3"},
            })
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            LOGGER.warning("TTS chunk hash=%s bytes=%d timeout after %dms", chunk_hash, chunk_bytes, elapsed_ms)
            if attempt >= TTS_MAX_RETRIES:
                raise RuntimeError(f"Google TTS timed out after {attempt + 1} attempts") from exc
            await asyncio.sleep(2 * (attempt + 1))
            continue

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            LOGGER.info("TTS chunk hash=%s bytes=%d status=200 elapsed=%dms", chunk_hash, chunk_bytes, elapsed_ms)
            return base64.b64decode(resp.json()["audioContent"])

        try:
            err = resp.json().get("error", {}).get("message", resp.text[:200])
        except ValueError:
            err = resp.text[:200]
        LOGGER.warning("TTS chunk hash=%s bytes=%d status=%d elapsed=%dms err=%s", chunk_hash, chunk_bytes, resp.status_code, elapsed_ms, err[:100])
        if resp.status_code in {429, 500, 502, 503, 504} and attempt < TTS_MAX_RETRIES:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        raise RuntimeError(f"Google TTS: {err}")

    raise RuntimeError("Google TTS failed")


def _parse_retry_delay(raw: Optional[str]) -> Optional[float]:
    try:
        return float(str(raw).rstrip("s")) if raw else None
    except ValueError:
        return None


async def _post_gemini(client: httpx.AsyncClient, text: str, persona: Optional[str] = None) -> bytes:
    """One generateContent call. Returns WAV bytes. Never calls countTokens (it spends the TPM quota).
    Body mirrors AI Studio Playground: "## Transcript:" header + speechMetadata.style + prebuilt voice."""
    p = PERSONA_BY_KEY.get(persona or DEFAULT_PERSONA, PERSONA_BY_KEY[DEFAULT_PERSONA])
    part: dict[str, Any] = {"text": "## Transcript:\n" + text}
    if p.style:
        part["speechMetadata"] = {"style": p.style}
    resp = await client.post(
        GEMINI_URL.format(model=GEMINI_MODEL),
        headers={"x-goog-api-key": GEMINI_API_KEY or ""},
        json={
            "contents": [{"role": "user", "parts": [part]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": p.voice}}},
            },
        },
    )
    if resp.status_code == 200:
        candidate = (resp.json().get("candidates") or [{}])[0]
        finish = candidate.get("finishReason")
        inline = next((p["inlineData"] for p in candidate.get("content", {}).get("parts", []) if "inlineData" in p), None)
        if inline is None:
            raise RuntimeError(f"Gemini TTS returned no audio (finishReason={finish})")
        if finish != "STOP":
            LOGGER.warning("Gemini TTS finishReason=%s — audio may be incomplete", finish)
        audio = base64.b64decode(inline["data"])
        if audio[:4] == b"RIFF":            # 3.8 returns audio/wav with its own header
            return audio
        buf = io.BytesIO()                  # raw L16 (older models / streaming) → wrap once
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(audio)
        return buf.getvalue()

    try:
        error = resp.json().get("error", {})
    except ValueError:
        error = {"message": resp.text[:200]}
    if resp.status_code == 429:
        quota_ids, delay = [], None
        for detail in error.get("details", []):
            quota_ids += [v.get("quotaId", "") for v in detail.get("violations", [])]
            delay = delay or _parse_retry_delay(detail.get("retryDelay"))
        if any("PerDay" in q for q in quota_ids):
            kind = "day"
        elif any("InputTokens" in q for q in quota_ids):
            kind = "tokens"
        else:
            kind = "minute"
        raise GeminiQuotaError(kind, delay, error.get("message", "quota exceeded")[:200])
    raise RuntimeError(f"Gemini TTS {resp.status_code}: {error.get('message', '')[:200]}")


MP3_SLICE_SECONDS = 30


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Encode in 30 s slices: feeding lameenc a whole 17-min part at once peaks at ~400 MB RSS."""
    out = bytearray()
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(GEMINI_MP3_KBPS)
        encoder.set_in_sample_rate(w.getframerate())
        encoder.set_channels(w.getnchannels())
        encoder.set_quality(2)
        slice_frames = w.getframerate() * MP3_SLICE_SECONDS
        while True:
            frames = w.readframes(slice_frames)
            if not frames:
                break
            out += encoder.encode(frames)
        out += encoder.flush()
    return bytes(out)


async def _gemini_segments(
    client: httpx.AsyncClient, text: str, pacer: GeminiPacer, persona: Optional[str] = None,
) -> tuple[list[bytes], int]:
    """Returns MP3 segments. Each WAV is encoded (off the event loop) as soon as it arrives, so
    at most one part's WAV is held in memory at a time."""
    await pacer.wait()
    try:
        wav = await _post_gemini(client, text, persona=persona)
        requests = 1
    except GeminiQuotaError as exc:
        if exc.kind == "minute":
            # Requests/min or unknown per-minute limit: same size will pass after the delay.
            await pacer._sleep(exc.retry_delay or pacer.min_interval)
            wav = await _post_gemini(client, text, persona=persona)
            requests = 2
        elif exc.kind == "tokens" and len(text) >= GEMINI_MIN_SPLIT * 2:
            # Counted over the input-token budget: this size can never pass — halve it.
            LOGGER.warning("Gemini input-token quota on %d chars — splitting in half", len(text))
            mp3s, requests = [], 1
            for half in _split_text(text, len(text) // 2):
                half_mp3s, half_requests = await _gemini_segments(client, half, pacer, persona)
                mp3s += half_mp3s
                requests += half_requests
            return mp3s, requests
        else:
            raise
    mp3 = await asyncio.to_thread(_wav_to_mp3, wav)
    del wav
    return [mp3], requests


async def _synthesize_gemini_part(text: str, pacer: GeminiPacer, persona: Optional[str] = None) -> tuple[bytes, int]:
    timeout = httpx.Timeout(GEMINI_READ_TIMEOUT, connect=TTS_CONNECT_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        mp3s, requests = await _gemini_segments(client, text, pacer, persona)
    return b"".join(mp3s), requests


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


async def _send_voice_with_timeout(
    app: Application,
    chat_id: int,
    voice: io.BytesIO,
    caption: str,
) -> None:
    try:
        await asyncio.wait_for(
            app.bot.send_voice(
                chat_id=chat_id,
                voice=voice,
                caption=caption,
                read_timeout=TELEGRAM_SEND_TIMEOUT,
                write_timeout=TELEGRAM_SEND_TIMEOUT,
                connect_timeout=10,
            ),
            timeout=TELEGRAM_SEND_TIMEOUT + 5,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"ส่งไฟล์เสียงเข้า Telegram เกิน {TELEGRAM_SEND_TIMEOUT:g} วิ") from exc


async def _safe_edit_or_send(app: Application, chat_id: int, message_id: int, text: str) -> None:
    try:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception:
        with contextlib.suppress(Exception):
            await app.bot.send_message(chat_id=chat_id, text=text)


def _fire_and_forget_edit(app: Application, chat_id: int, message_id: int, text: str) -> None:
    asyncio.create_task(_safe_edit_or_send(app, chat_id, message_id, text))


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

    engine = _engine()
    if engine == "gemini":
        parts = _split_gemini_parts(text)
        request_count = len(parts)
    else:
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
        engine=engine,
        persona=_resolve_persona(await _get_chat_persona(app, chat_id)),
    ))


async def _tts_worker(app: Application) -> None:
    queue = _get_queue()
    while True:
        job = await queue.get()
        try:
            await _process_job(app, job)
        except Exception as exc:
            LOGGER.exception("Unhandled TTS job failure")
            await _safe_edit_or_send(
                app,
                job.chat_id,
                job.status_message_id,
                f"เกิดข้อผิดพลาดในคิว TTS: {exc}",
            )
        finally:
            queue.task_done()


async def _process_job(app: Application, job: TTSJob) -> None:
    total_used = 0
    total_requests = 0
    summary = USAGE_METER.preview()
    parts = list(job.parts)
    engine = job.engine

    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        LOGGER.info("Processing TTS file %s/%s chars=%s engine=%s", index, len(parts), len(part), engine)
        await _safe_edit_or_send(
            app,
            job.chat_id,
            job.status_message_id,
            f"กำลังสร้างเสียงไฟล์ {index}/{len(parts)}...",
        )
        try:
            if engine == "gemini":
                try:
                    audio_bytes, requests = await _synthesize_gemini_part(part, GEMINI_PACER, persona=job.persona)
                except Exception as exc:
                    # Any Gemini failure (daily quota, 5xx, timeout, no audio): the rest of THIS article
                    # goes to Chirp, re-split for Chirp's limits. Told in its own message — a status
                    # edit would be overwritten by the next progress update.
                    LOGGER.warning("Gemini failed on file %s/%s (%s: %s) — Chirp for the rest", index, len(parts), type(exc).__name__, exc)
                    reason = "โควตา Gemini วันนี้หมดแล้ว" if isinstance(exc, GeminiQuotaError) and exc.kind == "day" \
                        else f"Gemini ใช้ไม่ได้ ({type(exc).__name__}: {str(exc)[:120] or 'no detail'})"
                    engine = "chirp"
                    parts = parts[: index - 1] + _split_output_parts("\n\n".join(parts[index - 1:]))
                    index -= 1
                    with contextlib.suppress(Exception):
                        await app.bot.send_message(chat_id=job.chat_id, text=f"{reason} — ไฟล์ที่เหลือใช้เสียง Chirp แทน")
                    continue
                used_chars = len(part)
                summary = USAGE_METER.record(used_chars)
            else:
                async def report_progress(done: int, total: int) -> None:
                    _fire_and_forget_edit(
                        app,
                        job.chat_id,
                        job.status_message_id,
                        f"กำลังสร้างเสียงไฟล์ {index}/{len(parts)} · ท่อน {done}/{total}...",
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
                f"เกิดข้อผิดพลาดตอนสร้างไฟล์ {index}/{len(parts)}: {exc}",
            )
            return

        total_used += used_chars
        total_requests += requests
        voice = io.BytesIO(audio_bytes)
        voice.name = f"tts_part_{index:02d}_of_{len(parts):02d}.mp3"
        voice_size = len(audio_bytes)
        _fire_and_forget_edit(
            app,
            job.chat_id,
            job.status_message_id,
            f"สร้างเสียงไฟล์ {index}/{len(parts)} เสร็จแล้ว กำลังส่งเข้า Telegram...",
        )
        try:
            send_start = time.monotonic()
            await _send_voice_with_timeout(
                app=app,
                chat_id=job.chat_id,
                voice=voice,
                caption=f"ไฟล์ {index}/{len(parts)} · {len(part):,} ตัวอักษร",
            )
            send_ms = int((time.monotonic() - send_start) * 1000)
            LOGGER.info("Sent voice file %s/%s size=%d elapsed=%dms", index, len(parts), voice_size, send_ms)
        except Exception as exc:
            send_ms = int((time.monotonic() - send_start) * 1000)
            LOGGER.error("Failed voice file %s/%s size=%d elapsed=%dms err=%s", index, len(parts), voice_size, send_ms, exc)
            await _safe_edit_or_send(
                app,
                job.chat_id,
                job.status_message_id,
                f"เกิดข้อผิดพลาดตอนส่งไฟล์ {index}/{len(parts)} เข้า Telegram: {exc}",
            )
            return

    await _safe_edit_or_send(
        app,
        job.chat_id,
        job.status_message_id,
        "เสร็จแล้ว\n" + _format_usage_summary(summary, total_used, total_requests),
    )

def _is_allowed_chat(chat_id: int) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    if chat_id in ALLOWED_CHAT_IDS:
        return True
    LOGGER.info("Ignored unauthorized chat_id=%s", chat_id)
    return False


async def start(update: Update, context):
    if not _is_allowed_chat(update.effective_chat.id):
        return
    await update.message.reply_text("ส่ง text มา แล้วจะแปลงเป็นเสียงให้ฟัง\n/settings เลือกผู้อ่าน")


async def settings(update: Update, context):
    chat_id = update.effective_chat.id
    if not _is_allowed_chat(chat_id):
        return
    current = await _get_chat_persona(context.application, chat_id)
    await update.message.reply_text("เลือกผู้อ่าน", reply_markup=_settings_keyboard(current))


async def on_persona_button(update: Update, context):
    chat_id = update.effective_chat.id
    query = update.callback_query
    if not _is_allowed_chat(chat_id):
        return
    key = (query.data or "").removeprefix("persona:")
    if key not in PERSONA_BY_KEY and key != RANDOM_PERSONA:
        await query.answer()
        return
    await _save_chat_persona(context.application, chat_id, key)
    await query.answer(f"เลือก {_persona_label(key)} แล้ว")
    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=_settings_keyboard(key))


async def status(update: Update, context):
    chat_id = update.effective_chat.id
    if not _is_allowed_chat(chat_id):
        return
    reader = _persona_label(await _get_chat_persona(context.application, chat_id))
    await update.message.reply_text(_format_runtime_status(USAGE_METER.preview()) + f"\nreader: {reader}")


async def handle_text(update: Update, context):
    chat_id = update.effective_chat.id
    if not _is_allowed_chat(chat_id):
        return

    text = update.message.text.strip()
    if not text:
        return
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
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CallbackQueryHandler(on_persona_button, pattern=r"^persona:"))
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
