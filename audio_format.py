"""format contract — ทุก engine ต้องคืนเสียงหน้าตาเดียวกัน

ทำไมต้องมีชั้นนี้ (วัดบน pc-office 2026-07-30 · 6 chunk ไทย · เครื่องว่าง):

    engine      integrated LUFS      true peak สูงสุด
    F5 local    -15.75 ± 0.50        +0.2 dBFS   ← เกินเต็มสเกลแล้ว
    Google      -18.72 ± 0.72        -0.1 dBFS

  1. ต่างกัน **3.0 LU** ⇒ ตอน fallback กลางบทความ ผู้ฟังได้ยินเสียงกระโดด
  2. F5 peak **เกิน 0 dBFS** ⇒ พอ encode เป็น MP3 จะคลิป (WAV float ซ่อนไว้ได้ MP3 ซ่อนไม่ได้)
  3. sd ในเครื่องเดียวกัน (0.50 / 0.72 LU) **แคบกว่าช่องว่างระหว่างเครื่องมาก**
     ⇒ "gain คงที่ต่อ engine" ชดเชยได้จริง — ข้อนี้วัดก่อนเลือก ไม่ใช่เลือกก่อนวัด

**ห้าม loudnorm ต่อ chunk**: loudnorm คิดเกนใหม่ทุกไฟล์ ⇒ chunk เงียบได้เกนเยอะ chunk ดังได้เกนน้อย
พอต่อกันจะได้ยิน "หายใจ" ขึ้นลงระหว่างไฟล์ เกนคงที่ผิดพลาดเท่ากันทุกไฟล์ — ซึ่งไม่มีใครได้ยิน

⚠️ กับดัก: `alimiter` มี `level=true` เป็น default (auto level) ⇒ ต้องปิดทุกครั้ง
ผลที่ **วัดได้จริง** ของการเปิดทิ้งไว้คือความดังขยับ **+1.0 dB เท่ากันทุกระดับ** (makeup = 1/limit)
ไม่ใช่ "รีนอร์มไลซ์ให้ดังเต็ม" อย่างที่เดาไว้ตอนแรก — ต่างกันตรงที่ผลแบบแรกทำให้
เทสต์ที่วัด *ผลต่าง* ระหว่างสองสัญญาณมองไม่เห็นเลย ด่านจึงต้องวัด **ค่าสัมบูรณ์**
(`tests/test_audio_format.py::test_ห้าม_auto_level_ของ_alimiter` — ซ้อมด่านแล้วว่าปฏิเสธจริง)
"""
import asyncio
import json
import subprocess
from dataclasses import dataclass
from typing import Optional

# ---------- contract ----------
SAMPLE_RATE = 24000        # F5 ออกมาที่นี่อยู่แล้ว — resample หนึ่งครั้งที่ Google ถูกกว่าที่ F5
CHANNELS = 1
BITRATE = "128k"           # 20 นาที ~19.2 MB · เพดาน Telegram 50 MB ที่ ~52 นาที
CODEC = "mp3"
NOMINAL_TARGET_LUFS = -16.0    # ค่าที่ใช้ *คำนวณเกน* = ระดับธรรมชาติของ F5 (เส้นหลักแปรรูปน้อยสุด)
LIMIT_DBTP = -1.0

# ค่าที่ **ลงจริงหลัง encode** — ไม่เท่ากับ nominal เพราะ limiter ดึง peak ลง ~0.7 dB อย่างเป็นระบบ
# วัด 2026-07-30 · 6 chunk ต่อ engine · เครื่องว่าง:
#     F5     -16.68 ± 0.40 LUFS
#     Google -16.80 ± 0.60 LUFS
#     ช่องว่างระหว่าง engine  3.00 LU  ->  **0.12 LU**   ← ตัวเลขที่ contract นี้มีไว้เพื่อทำให้เกิด
# เขียนค่าที่วัดได้ไว้ตรงนี้แทนที่จะแก้เกนไล่ตาม nominal เพราะสิ่งที่ผู้ฟังได้ยินคือ *ช่องว่าง*
# ไม่ใช่ระดับสัมบูรณ์ และการดันเกนขึ้นอีก 0.7 dB มีแต่ทำให้ limiter ทำงานหนักขึ้นเปล่าๆ
MEASURED_LUFS = {"local-f5": -16.68, "google": -16.80}
MEASURED_SPREAD_LU = 0.12

# encode เพิ่มความยาว +0.048…+0.061 s ต่อไฟล์ (encoder delay padding ของ LAME = ความเงียบหัวไฟล์)
# ไฟล์ยาวไฟล์เดียวไม่มีผล แต่เส้น fallback ที่หั่นเป็น part จะ **สะสม** ที่รอยต่อพอดี
# ⇒ ต้องวัดตอนงาน long-form ห้ามสรุปตอนนี้ว่าไม่มีผล
ENCODE_PADDING_SECONDS = 0.06

# เกนคงที่ต่อ engine = NOMINAL_TARGET_LUFS - ค่าดิบที่วัดได้ (ψ/lab/loudness_survey.py ฝั่ง AgentP-oracle)
# ถ้าเปลี่ยนโมเดล/เสียง/voice ของ Google ⇒ **ต้องวัดใหม่** ตัวเลขนี้ผูกกับ setup ที่วัด ไม่ใช่ค่าสากล
ENGINE_GAIN_DB = {
    "local-f5": -0.3,      # -16.0 - (-15.75)
    "google": +2.7,        # -16.0 - (-18.72)
}


class FormatError(RuntimeError):
    """encode ไม่สำเร็จ — งานล้ม ไม่ใช่ 'ส่งต่อ engine ถัดไป'"""


@dataclass(frozen=True)
class AudioInfo:
    codec: str
    sample_rate: int
    channels: int
    seconds: float
    samples: int           # นับจริงจากการ decode ไม่ใช่ seconds * rate ที่ปัดเศษ
    bytes: int

    def matches_contract(self) -> bool:
        return (self.codec == CODEC
                and self.sample_rate == SAMPLE_RATE
                and self.channels == CHANNELS)


def _ffmpeg_args(gain_db: float) -> list:
    limit_linear = 10 ** (LIMIT_DBTP / 20.0)          # -1 dBTP -> 0.891
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-af", (f"volume={gain_db}dB,"
                f"alimiter=limit={limit_linear:.6f}:level=disabled:latency=true"),
        "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "-c:a", "libmp3lame", "-b:a", BITRATE,
        "-f", "mp3", "pipe:1",
    ]


def encode_sync(raw: bytes, gain_db: float) -> bytes:
    proc = subprocess.run(_ffmpeg_args(gain_db), input=raw, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise FormatError(f"ffmpeg ล้ม rc={proc.returncode}: {proc.stderr.decode()[:300]}")
    return proc.stdout


async def encode(raw: bytes, gain_db: float) -> bytes:
    """encode ไม่บล็อก event loop — ffmpeg เป็นโปรเซสแยกอยู่แล้ว"""
    proc = await asyncio.create_subprocess_exec(
        *_ffmpeg_args(gain_db),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(raw)
    if proc.returncode != 0 or not out:
        raise FormatError(f"ffmpeg ล้ม rc={proc.returncode}: {err.decode()[:300]}")
    return out


def encode_for(engine_name: str, raw: bytes) -> bytes:
    return encode_sync(raw, ENGINE_GAIN_DB.get(engine_name, 0.0))


def probe(data: bytes) -> AudioInfo:
    """อ่านของจริงจาก bytes — ไม่เชื่อว่า engine คืนอะไรมาเพราะชื่อฟังก์ชันบอกว่า mp3"""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", "pipe:0"],
        input=data, capture_output=True,
    )
    if proc.returncode != 0:
        raise FormatError(f"ffprobe อ่านไม่ออก: {proc.stderr.decode()[:300]}")
    meta = json.loads(proc.stdout)
    stream = next((s for s in meta["streams"] if s.get("codec_type") == "audio"), None)
    if stream is None:
        raise FormatError("ไม่มี audio stream ในข้อมูลนี้")
    # ffprobe บน pipe บอก duration ของ MP3 ไม่ได้ (seek ไม่ได้) — และถึงบอกได้ก็เป็นค่าจาก header
    # ⇒ นับจาก sample ที่ decode ออกมาจริงเสมอ ตัวเลขเดียว ไม่มีสองแหล่งให้ขัดกัน
    samples = count_samples(data)
    return AudioInfo(
        codec=stream["codec_name"],
        sample_rate=int(stream["sample_rate"]),
        channels=int(stream["channels"]),
        seconds=samples / SAMPLE_RATE,
        samples=samples,
        bytes=len(data),
    )


def count_samples(data: bytes) -> int:
    """decode จริงแล้วนับ sample — ตัวเลขนี้จับ 'ไฟล์เปิดได้แต่ไม่มีเสียง' ที่ header จับไม่ได้"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=data, capture_output=True,
    )
    if proc.returncode != 0:
        raise FormatError(f"decode ไม่ผ่าน: {proc.stderr.decode()[:300]}")
    return len(proc.stdout) // 2        # s16le = 2 ไบต์ต่อ sample


def measure_loudness(data: bytes) -> Optional[float]:
    """integrated LUFS — ใช้ในเทสต์ ไม่ได้อยู่บนเส้นทางจริงตอนส่งงาน"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", "pipe:0",
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        input=data, capture_output=True,
    )
    tail = proc.stderr.decode()
    marker = tail.rfind("Integrated loudness")
    if marker < 0:
        return None
    for line in tail[marker:].splitlines():
        if line.strip().startswith("I:"):
            return float(line.split()[1])
    return None
