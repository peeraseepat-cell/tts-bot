"""ชั้น engine — local-first แล้ว fallback ไป Google

ทำไมแยกไฟล์จาก bot.py (ซึ่งเป็น single-file by design):
LocalF5Engine ต้อง import torch + f5_tts_th ซึ่งหนักหลายวินาที และไม่จำเป็นเลย
ถ้า deploy แบบ Google-only ⇒ import แบบ lazy ในไฟล์แยก ทำให้ bot.py ไม่ต้องแบก path นั้น

policy ที่ตัวเลขบังคับ (ไม่ใช่ที่เลือกเพราะสวย) — วัดบน RTX 5070 Ti 2026-07-30:
  Google  ขนาน 8 ⇒ throughput 6.44 req/s · speedup 7.02x (network I/O-bound)
  F5      ขนาน 8 ด้วย thread ⇒ 0.352 req/s · speedup 0.34x (**ติดลบ**)
          ขนานด้วยหลายโปรเซส ⇒ +15% แลก latency 3.5 เท่า
⇒ max_concurrency เป็นสมบัติ **ของ engine** ไม่ใช่ค่าเดียวของระบบ
รายละเอียด: AgentP-oracle/ψ/memory/learnings/2026-07-30_f5-concurrency-ram-ceiling.md
             AgentP-oracle/ψ/memory/learnings/2026-07-30_google-vs-f5-crossover.md
"""
import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx

import audio_format

LOGGER = logging.getLogger(__name__)


class EngineUnavailable(Exception):
    """engine นี้รับงานชิ้นนี้ไม่ได้ — ให้ตัวถัดไปใน chain ลอง

    ต่างจาก RuntimeError: อันนี้คือ "ส่งต่อได้" ไม่ใช่ "งานล้ม"
    ต้องยกเมื่อ: ไม่มี GPU · VRAM ไม่พอ · ref audio หาย · **ด่าน translit ตีตก**
    """


@dataclass(frozen=True)
class VoiceSpec:
    """เสียงของ agent หนึ่งตัว — ผูก engine ไว้กับเสียง ไม่ใช่ผูกกับระบบ

    เสียงของ Boommer ทำได้เฉพาะ F5 (โคลนจาก ref) ⇒ engine="local"
    เสียง agent อื่นที่ยังไม่มี ref ⇒ engine="google" ไปก่อนได้ โดยไม่แก้ design
    """

    agent: str
    engine: str                              # "local" | "google"
    google_voice: Optional[str] = None       # ใช้เมื่อ engine == "google"
    ref_wav: Optional[str] = None            # ใช้เมื่อ engine == "local"
    ref_text: Optional[str] = None


class TTSEngine(Protocol):
    """สัญญาของ engine ทุกตัว

    `-> bytes` เป็นสัญญาที่ว่างเปล่า — bytes อะไรก็ผ่าน type checker ได้หมด
    (บทเรียน 2026-07-30: concat ข้าม format ทำให้เสียงหายเงียบ โดยไม่มี error สักบรรทัด)
    ⇒ สัญญาจริงเขียนไว้ตรงนี้ และ **บังคับด้วยเทสต์ที่ decode ของจริง** ไม่ใช่ด้วยชนิดข้อมูล:

        synth() ต้องคืน MP3 · 24 kHz · mono · 128 kbps · ผ่านเกนคงที่ของ engine ตัวเองแล้ว
        ⇒ ต่อกันได้โดยไม่ต้องแปลงอีก และสลับ engine กลางบทความแล้วความดังไม่กระโดด
    """

    name: str
    max_concurrency: int

    async def synth(self, chunk: str, voice: VoiceSpec) -> bytes:
        ...


class GoogleEngine:
    """path เดิมของ bot ทั้งดุ้น — ย้ายมาไว้หลัง interface ไม่เปลี่ยนพฤติกรรม"""

    name = "google"

    URL = "https://texttospeech.googleapis.com/v1/text:synthesize"

    def __init__(self, api_key: str, default_voice: str, max_concurrency: int = 4,
                 connect_timeout: float = 10.0, read_timeout: float = 15.0,
                 max_retries: int = 0) -> None:
        # key อยู่ใน **header** ไม่ใช่ query string — httpx log บรรทัด
        # "HTTP Request: POST <url>" ที่ระดับ INFO ⇒ key ใน ?key= จะไหลลง log ทุกครั้งที่ยิง
        # (พี่ AgentA จับได้ตอน review PR B — ของเดิม carry มาจาก bot.py ไม่ใช่ regression)
        self._headers = {"X-Goog-Api-Key": api_key}
        self._default_voice = default_voice
        self.max_concurrency = max_concurrency
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """client ตัวเดียวใช้ซ้ำ — เปิดใหม่ทุก chunk เสียเวลา handshake +3% ต่อ chunk (วัดแล้ว)"""
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(timeout=self._timeout,
                                                     headers=self._headers)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def synth(self, chunk: str, voice: VoiceSpec) -> bytes:
        name = voice.google_voice or self._default_voice
        lang = name.rsplit("-Chirp3", 1)[0]
        payload = {
            "input": {"text": chunk},
            "voice": {"languageCode": lang, "name": name},
            "audioConfig": {"audioEncoding": "MP3"},
        }
        client = await self._get_client()
        for attempt in range(self._max_retries + 1):
            start = time.monotonic()
            try:
                resp = await client.post(self.URL, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_retries:
                    raise EngineUnavailable(f"Google TTS ติดต่อไม่ได้: {exc}") from exc
                await asyncio.sleep(2 * (attempt + 1))
                continue

            elapsed_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code == 200:
                LOGGER.info("engine=google voice=%s bytes=%d elapsed=%dms",
                            name, len(chunk.encode()), elapsed_ms)
                raw = base64.b64decode(resp.json()["audioContent"])
                return await audio_format.encode(raw, audio_format.ENGINE_GAIN_DB[self.name])

            try:
                err = resp.json().get("error", {}).get("message", resp.text[:200])
            except ValueError:
                err = resp.text[:200]
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            # 4xx ที่ไม่ใช่ 429 = คำขอผิด ไม่ใช่ engine ล่ม ⇒ ไม่ส่งต่อ chain ให้ล้มเลย
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise RuntimeError(f"Google TTS: {err}")
            raise EngineUnavailable(f"Google TTS: {err}")
        raise EngineUnavailable("Google TTS ล้มหลังลองครบ")


class LocalF5Engine:
    """F5-TTS-THAI บน GPU เครื่องนี้ — serialize **ตั้งใจ** ไม่ใช่เพราะยังไม่ได้ optimize

    max_concurrency = 1 มาจากผลวัด 2 ชุด (thread ติดลบ · process +15% แลก latency 3.5x)
    ถ้าจะเพิ่มค่านี้ ต้องมีผลวัดใหม่มาแทน ไม่ใช่เพิ่มเพราะดูน้อย
    """

    name = "local-f5"
    max_concurrency = 1

    def __init__(self, model: str = "v1", step: int = 32, cfg: float = 2.0,
                 speed: float = 1.0, sample_rate: int = 24000) -> None:
        self._model_name = model
        self._step, self._cfg, self._speed = step, cfg, speed
        self._sample_rate = sample_rate
        self._tts = None
        self._lock = asyncio.Lock()          # ← serialize; ไม่ใช่ pool
        self._load_failed: Optional[str] = None

    # ---------- ด่านก่อนถึงโมเดล ----------
    @staticmethod
    def check_text(chunk: str) -> None:
        """อักษรละติน/เลขอารบิกที่เหลือ = F5 จะสะกดทีละตัว หรือ **ข้ามเงียบ**

        วัดแล้ว: 'merge PR ... main' → STT ถอดได้ '122 comment' (หาย 2 คำ เพี้ยน 2)
        ⇒ ต้อง raise ไม่ใช่ warning — ด่านที่ไม่หยุดการกระทำคือป้าย ไม่ใช่ประตู
        """
        from thai_text import residual_digits, residual_latin

        latin = residual_latin(chunk)
        digits = residual_digits(chunk)
        if latin or digits:
            raise EngineUnavailable(
                f"ด่าน translit ตีตก — ละตินเหลือ {latin} เลขเหลือ {digits}"
            )

    def _ensure_model(self):
        if self._load_failed:
            raise EngineUnavailable(f"โหลดโมเดลไม่สำเร็จก่อนหน้านี้: {self._load_failed}")
        if self._tts is not None:
            return self._tts
        try:
            import soundfile as sf
            import torch
            import torchaudio

            def _sf_load(filepath, *a, **k):
                data, sr = sf.read(str(filepath), dtype="float32", always_2d=True)
                return torch.from_numpy(data.T).contiguous(), sr

            torchaudio.load = _sf_load          # ต้องมาก่อน import f5_tts_th เสมอ
            from f5_tts_th.tts import TTS

            if not torch.cuda.is_available():
                raise RuntimeError("ไม่มี CUDA — F5 บน CPU ช้าเกินใช้งาน")
            start = time.monotonic()
            self._tts = TTS(model=self._model_name)
            LOGGER.info("engine=local-f5 โหลดโมเดล %.2fs", time.monotonic() - start)
            return self._tts
        except Exception as exc:
            self._load_failed = f"{type(exc).__name__}: {exc}"
            raise EngineUnavailable(f"โหลด F5 ไม่ได้: {exc}") from exc

    def _infer_to_wav(self, chunk: str, voice: VoiceSpec) -> bytes:
        """คืน WAV ดิบจากโมเดล — ยังไม่ใช่ของที่ส่งออก ต้องผ่าน audio_format ก่อน

        ชื่อเดิมคือ `_infer_to_mp3` แต่คืน WAV — พี่ AgentA จับได้ตอน review PR B
        ชื่อที่โกหกคือหนี้ที่รอวันหลอกคนอ่านคนถัดไป แก้ตอนนี้พร้อมกับที่ format contract ล็อก
        """
        import io

        tts = self._ensure_model()          # ก่อน import หนัก — เหตุผลเดียวกับ _infer_long_to_wav
        try:
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            raise EngineUnavailable(f"ไม่มี dependency ของ local engine: {exc}") from exc

        wav = tts.infer(ref_audio=voice.ref_wav, ref_text=voice.ref_text, gen_text=chunk,
                        step=self._step, cfg=self._cfg, speed=self._speed)
        arr = np.asarray(wav, dtype="float32").squeeze()
        buf = io.BytesIO()
        sf.write(buf, arr, self._sample_rate, format="WAV")
        return buf.getvalue()

    async def synth(self, chunk: str, voice: VoiceSpec) -> bytes:
        if voice.engine != "local":
            raise EngineUnavailable(f"voice ของ {voice.agent} ไม่ได้ตั้งให้ใช้ local")
        if not voice.ref_wav or not voice.ref_text:
            raise EngineUnavailable(f"voice ของ {voice.agent} ไม่มี ref audio/text")
        if not os.path.exists(voice.ref_wav):
            raise EngineUnavailable(f"ref audio หายจากดิสก์: {voice.ref_wav}")
        self.check_text(chunk)
        async with self._lock:
            start = time.monotonic()
            raw = await asyncio.to_thread(self._infer_to_wav, chunk, voice)
            LOGGER.info("engine=local-f5 agent=%s chars=%d elapsed=%dms",
                        voice.agent, len(chunk), int((time.monotonic() - start) * 1000))
        # encode นอก lock — ffmpeg ไม่แตะ GPU ให้ chunk ถัดไปเข้าโมเดลได้เลย
        return await audio_format.encode(raw, audio_format.ENGINE_GAIN_DB[self.name])

    # ---------- เส้นยาวไฟล์เดียว (AMEND-2) ----------
    def _infer_long_to_wav(self, text: str, voice: VoiceSpec) -> tuple:
        """หั่นเอง -> infer ทีละก้อน -> ต่อด้วยความเงียบที่คุมเอง -> WAV ก้อนเดียว

        ทำไมไม่โยนทั้งก้อนให้ `TTS.infer()` หั่นเอง ทั้งที่เร็วกว่า 22% —
        **วัดที่ 1/5/20 นาที แล้ว VRAM ของสองวิธีคนละพฤติกรรม**:
            lib   889 -> 1222 -> 2341 MB   (โตตามความยาวข้อความ)
            หั่นเอง 801 ->  804 ->  806 MB   (คงที่)
        งบ VRAM ที่เหลือให้ LLM บนเครื่องนี้คือ ~12.8 GB ⇒ เส้นที่โตตาม input
        จะเอาเพดานความยาวบทความไปผูกกับ LLM ที่ยังไม่ได้ลง
        (ref drift วัดแล้ว **ไม่มีทั้งสองวิธี** ตลอด 20 นาที ⇒ ไม่ใช่เกณฑ์ตัดสิน)
        """
        import io

        from thai_text import LIB_MAX_CHARS, MAX_CHUNK, plan

        # ลำดับสำคัญ: ตรวจสถานะโมเดล **ก่อน** แตะ dependency หนัก
        # ถ้า import numpy/soundfile ก่อน เครื่องที่ไม่มีของพวกนี้ (deploy แบบ Google-only)
        # จะได้ ModuleNotFoundError ซึ่ง **ไม่ใช่ EngineUnavailable** ⇒ ไม่ตกไป fallback
        # แต่ทำให้ทั้ง job ล้ม — เทสต์ซ้อมด่านข้อ 3 จับได้ตอนเขียน ไม่ใช่ตอน deploy
        tts = self._ensure_model()
        try:
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            raise EngineUnavailable(f"ไม่มี dependency ของ local engine: {exc}") from exc

        items, stat = plan(text, MAX_CHUNK)
        if not items:
            raise EngineUnavailable("ไม่มีข้อความให้อ่านหลังหั่น")
        if stat["max_chunk"] > LIB_MAX_CHARS:
            # ด่านนี้กันไม่ให้ lib หั่นซ้ำก้อนของเรา — ถ้าโดนหั่นซ้ำ เราคุม pause ไม่ได้แล้ว
            raise EngineUnavailable(
                f"ก้อนยาว {stat['max_chunk']} เกิน max_chars ที่ส่งให้ lib ({LIB_MAX_CHARS})")

        pieces = []
        for chunk, gap in items:
            wav = tts.infer(ref_audio=voice.ref_wav, ref_text=voice.ref_text, gen_text=chunk,
                            step=self._step, cfg=self._cfg, speed=self._speed,
                            max_chars=LIB_MAX_CHARS)
            pieces.append(np.asarray(wav, dtype="float32").squeeze())
            if gap:
                pieces.append(np.zeros(int(gap * self._sample_rate), dtype="float32"))
        arr = np.concatenate(pieces)
        buf = io.BytesIO()
        sf.write(buf, arr, self._sample_rate, format="WAV")
        return buf.getvalue(), stat, len(arr) / self._sample_rate

    async def synth_long(self, text: str, voice: VoiceSpec) -> "LongSynthResult":
        """ทั้ง job เป็นไฟล์เดียว — ไม่แบ่ง part (AMEND-2)

        ด่าน translit ตรวจ **ทั้งข้อความ** ก่อนแตะโมเดล: ถ้ามีละตินตกค้างแม้จุดเดียว
        ตกทั้ง job ไปเส้น fallback — ไม่ใช่ปล่อยครึ่งเสียงครึ่งเงียบ
        """
        if voice.engine != "local":
            raise EngineUnavailable(f"voice ของ {voice.agent} ไม่ได้ตั้งให้ใช้ local")
        if not voice.ref_wav or not voice.ref_text:
            raise EngineUnavailable(f"voice ของ {voice.agent} ไม่มี ref audio/text")
        if not os.path.exists(voice.ref_wav):
            raise EngineUnavailable(f"ref audio หายจากดิสก์: {voice.ref_wav}")
        self.check_text(text)
        async with self._lock:
            start = time.monotonic()
            raw, stat, seconds = await asyncio.to_thread(self._infer_long_to_wav, text, voice)
            wall = time.monotonic() - start
            LOGGER.info("engine=local-f5 long agent=%s chars=%d chunks=%d hard_cuts=%d "
                        "audio=%.1fs wall=%.1fs rtf=%.4f",
                        voice.agent, len(text), stat["chunks"], stat["hard_cuts"],
                        seconds, wall, wall / seconds if seconds else float("nan"))
        audio = await audio_format.encode(raw, audio_format.ENGINE_GAIN_DB[self.name])
        return LongSynthResult(audio=audio, engine=self.name, seconds=seconds,
                               chunks=stat["chunks"], hard_cuts=stat["hard_cuts"])


@dataclass
class LongSynthResult:
    audio: bytes
    engine: str
    seconds: float
    chunks: int
    hard_cuts: int


@dataclass
class SynthResult:
    audio: bytes
    engine: str
    fell_back: bool
    reason: Optional[str] = None


class EngineChain:
    """ลองตามลำดับ — EngineUnavailable = ส่งต่อ · RuntimeError = ล้มทันที ไม่กลืน"""

    def __init__(self, engines: list, on_fallback=None) -> None:
        if not engines:
            raise ValueError("chain ต้องมี engine อย่างน้อยหนึ่งตัว")
        self._engines = engines
        self._on_fallback = on_fallback

    @property
    def primary(self):
        return self._engines[0]

    async def synth(self, chunk: str, voice: VoiceSpec) -> SynthResult:
        reasons = []
        for index, engine in enumerate(self._engines):
            try:
                audio = await engine.synth(chunk, voice)
            except EngineUnavailable as exc:
                reasons.append(f"{engine.name}: {exc}")
                LOGGER.warning("engine=%s ส่งต่อ chain: %s", engine.name, exc)
                continue
            if index > 0 and self._on_fallback:
                # ผู้ฟังต้องรู้ว่ากำลังฟังเสียงใคร — fallback เงียบแบบ UsageMeter ใช้ที่นี่ไม่ได้
                self._on_fallback(engine.name, "; ".join(reasons))
            return SynthResult(audio=audio, engine=engine.name,
                               fell_back=index > 0,
                               reason="; ".join(reasons) or None)
        raise RuntimeError("ไม่มี engine ไหนรับงานได้: " + "; ".join(reasons))
