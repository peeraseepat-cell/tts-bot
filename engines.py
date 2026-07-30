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
    name: str
    max_concurrency: int

    async def synth(self, chunk: str, voice: VoiceSpec) -> bytes:
        ...


class GoogleEngine:
    """path เดิมของ bot ทั้งดุ้น — ย้ายมาไว้หลัง interface ไม่เปลี่ยนพฤติกรรม"""

    name = "google"

    def __init__(self, api_key: str, default_voice: str, max_concurrency: int = 4,
                 connect_timeout: float = 10.0, read_timeout: float = 15.0,
                 max_retries: int = 0) -> None:
        self._url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={api_key}"
        self._default_voice = default_voice
        self.max_concurrency = max_concurrency
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._max_retries = max_retries

    async def synth(self, chunk: str, voice: VoiceSpec) -> bytes:
        name = voice.google_voice or self._default_voice
        lang = name.rsplit("-Chirp3", 1)[0]
        payload = {
            "input": {"text": chunk},
            "voice": {"languageCode": lang, "name": name},
            "audioConfig": {"audioEncoding": "MP3"},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for attempt in range(self._max_retries + 1):
                start = time.monotonic()
                try:
                    resp = await client.post(self._url, json=payload)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= self._max_retries:
                        raise EngineUnavailable(f"Google TTS ติดต่อไม่ได้: {exc}") from exc
                    await asyncio.sleep(2 * (attempt + 1))
                    continue

                elapsed_ms = int((time.monotonic() - start) * 1000)
                if resp.status_code == 200:
                    LOGGER.info("engine=google voice=%s bytes=%d elapsed=%dms",
                                name, len(chunk.encode()), elapsed_ms)
                    return base64.b64decode(resp.json()["audioContent"])

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

    def _infer_to_mp3(self, chunk: str, voice: VoiceSpec) -> bytes:
        import io

        import numpy as np
        import soundfile as sf

        tts = self._ensure_model()
        wav = tts.infer(ref_audio=voice.ref_wav, ref_text=voice.ref_text, gen_text=chunk,
                        step=self._step, cfg=self._cfg, speed=self._speed)
        arr = np.asarray(wav, dtype="float32").squeeze()
        buf = io.BytesIO()
        # ส่ง WAV ออกจาก engine — bot ส่งเข้า Telegram ได้ตรงๆ
        # loudnorm/mp3 เป็นขั้น encode แยก ไม่ใช่หน้าที่ของ engine
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
            audio = await asyncio.to_thread(self._infer_to_mp3, chunk, voice)
            LOGGER.info("engine=local-f5 agent=%s chars=%d elapsed=%dms",
                        voice.agent, len(chunk), int((time.monotonic() - start) * 1000))
            return audio


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
