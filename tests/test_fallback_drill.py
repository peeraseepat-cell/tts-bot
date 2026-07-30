"""ซ้อม fallback ทั้งเส้น — F5 ตาย/ด่านตีตก/VRAM ไม่พอ แล้วไหลลง Google จริง

ต่างจาก `test_local_wiring.py` ตรงที่ไฟล์นั้นซ้อม **ด่านทีละบาน** ส่วนไฟล์นี้ซ้อม **ทั้งเส้นทาง**:
ผู้ใช้ส่งข้อความ → local ปฏิเสธ → Google ทำงาน → ผู้ใช้ได้ไฟล์ **ที่ผ่าน format contract**
+ ได้รับแจ้งว่ากำลังฟังเสียงสำรอง

ข้อที่สำคัญที่สุดในไฟล์นี้คือข้อสุดท้าย: **ความดังปลายทางของสองเส้นต้องมาบรรจบกัน**
ถ้าตรวจแค่ว่า "ไหลลง Google แล้ว" จะพลาดกรณีที่ไหลถูกแต่เสียงกระโดด — ซึ่งเคยเป็นของจริง:
เส้น Google ไม่ได้ผ่าน audio_format จนกระทั่ง drill นี้จับได้
"""
import asyncio
import base64
import os
import subprocess
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import audio_format as af  # noqa: E402
import bot  # noqa: E402
import engines  # noqa: E402


def google_like_mp3(seconds: float = 2.0, level_db: float = -18.72) -> bytes:
    """เสียงที่ดังเท่า Google ของจริงที่วัดได้ (-18.72 LUFS) เพื่อให้ drill มีความหมาย"""
    amp = 10 ** (level_db / 20) * 3.0
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=200:duration={seconds}:sample_rate=24000",
         "-af", f"volume={amp}", "-ac", "1", "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
        capture_output=True, check=True).stdout


class Recorder:
    """จับทุกอย่างที่ bot ส่งออกไปหาผู้ใช้"""

    def __init__(self):
        self.texts = []
        self.files = []

    class _Bot:
        def __init__(self, rec):
            self.rec = rec

        async def edit_message_text(self, **k):
            self.rec.texts.append(k.get("text", ""))

        async def send_message(self, **k):
            self.rec.texts.append(k.get("text", ""))

        async def send_voice(self, **k):
            voice = k["voice"]
            voice.seek(0)
            self.rec.files.append((voice.read(), k.get("caption", "")))

    @property
    def app(self):
        rec = self

        class App:
            bot = Recorder._Bot(rec)

        return App()


def make_job(text="ข้อความไทยล้วนสำหรับทดสอบ"):
    return bot.TTSJob(chat_id=1, status_message_id=2, text=text,
                      parts=[text], queued_at=bot.datetime.now())


def run_with_google(monkeypatch, local_engine, text="ข้อความไทยล้วนสำหรับทดสอบ"):
    """เปิด local · ให้ Google ตอบด้วยเสียงจริงผ่าน MockTransport · ไม่แตะเน็ตจริง"""
    monkeypatch.setattr(bot, "USE_LOCAL_TTS", True)
    monkeypatch.setattr(bot, "_get_local_engine",
                        lambda: (local_engine, engines.VoiceSpec(
                            agent="Boommer", engine="local",
                            ref_wav=__file__, ref_text="อ้างอิง")))

    payload = google_like_mp3()

    def handler(request):
        return httpx.Response(200, json={"audioContent": base64.b64encode(payload).decode()})

    real_client = httpx.AsyncClient

    def fake_client(*a, **k):
        k.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(bot.httpx, "AsyncClient", fake_client)

    rec = Recorder()
    asyncio.run(bot._process_job(rec.app, make_job(text)))
    return rec, payload


# ---------- drill 1: ด่าน translit ตีตก ----------

def test_drill_ด่าน_translit_ตีตก_แล้วไหลลง_google_ครบเส้น(monkeypatch):
    rec, _ = run_with_google(monkeypatch, engines.LocalF5Engine(),
                             text="ข้อความนี้มีคำว่า merge PR ปนอยู่")
    assert rec.files, "ไม่มีไฟล์ถึงผู้ใช้เลย — เส้น fallback ขาด"
    assert any("เสียงสำรอง" in t for t in rec.texts), \
        f"ไหลลง Google แล้วแต่ไม่บอกผู้ฟังว่าเปลี่ยนเสียง: {rec.texts}"
    assert any("translit" in t for t in rec.texts), "ไม่บอกสาเหตุจริง"


# ---------- drill 2: โมเดลตาย / VRAM ไม่พอ ----------

@pytest.mark.parametrize("reason", [
    "OutOfMemoryError: CUDA out of memory",
    "RuntimeError: ไม่มี CUDA — F5 บน CPU ช้าเกินใช้งาน",
])
def test_drill_โมเดลตายแล้วไหลลง_google(monkeypatch, reason):
    engine = engines.LocalF5Engine()
    engine._load_failed = reason
    rec, _ = run_with_google(monkeypatch, engine)
    assert rec.files, "โมเดลตายแล้วผู้ใช้ไม่ได้อะไรเลย"
    assert any("เสียงสำรอง" in t for t in rec.texts)


# ---------- drill 3: ref หายจากดิสก์ ----------

def test_drill_ref_หายแล้วไหลลง_google(monkeypatch):
    monkeypatch.setattr(bot, "USE_LOCAL_TTS", True)
    monkeypatch.setattr(bot, "_get_local_engine",
                        lambda: (engines.LocalF5Engine(), engines.VoiceSpec(
                            agent="Boommer", engine="local",
                            ref_wav="/ไม่มีไฟล์นี้.wav", ref_text="อ้างอิง")))
    payload = google_like_mp3()

    def handler(request):
        return httpx.Response(200, json={"audioContent": base64.b64encode(payload).decode()})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(bot.httpx, "AsyncClient",
                        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    rec = Recorder()
    asyncio.run(bot._process_job(rec.app, make_job()))
    assert rec.files
    assert any("ref audio หาย" in t for t in rec.texts), rec.texts


# ---------- drill 4: ⭐ ความดังปลายทางของสองเส้นต้องบรรจบ ----------

def test_drill_เสียงสำรองต้องผ่าน_format_contract_เหมือนเส้นหลัก(monkeypatch):
    """ข้อที่จับของจริงได้: contract เคยติดอยู่บนเส้น local เส้นเดียว

    ถ้า fallback ส่ง Google ดิบออกไป ผู้ฟังจะได้ยินเสียงเบาลง ~3 LU ตรงจังหวะสลับ engine พอดี
    — ซึ่งเป็นจังหวะเดียวที่ contract มีไว้เพื่อมัน
    """
    rec, raw_google = run_with_google(monkeypatch, engines.LocalF5Engine(),
                                      text="ข้อความมี merge PR ทำให้ตกไป fallback")
    audio, _ = rec.files[0]
    info = af.probe(audio)
    assert info.matches_contract(), f"เสียงสำรองไม่เข้า contract: {info}"

    # วัด **ส่วนที่ขยับ** ไม่ใช่ค่าสัมบูรณ์ — ตรงนี้ต่างจากด่าน auto-level ที่ต้องวัดค่าสัมบูรณ์
    # เหตุผล: สัญญาณทดสอบเป็นไซน์ 200 Hz ซึ่ง K-weighting ของ LUFS กดความถี่ต่ำแรง
    # ⇒ ค่าสัมบูรณ์ของมันเทียบกับ -18.72 ที่วัดจาก **เสียงพูดจริง** ไม่ได้
    # หลักคือ "วัดสิ่งที่ bug จะขยับ": bug ที่กลัวตรงนี้คือ "ไม่ได้ใส่เกนของ engine"
    # ซึ่งเป็นการเลื่อนแบบสัมพัทธ์ ⇒ วัดสัมพัทธ์ · ส่วน auto-level เลื่อนทุกฝั่งเท่ากัน ⇒ วัดสัมบูรณ์
    raw_lufs = af.measure_loudness(raw_google)
    out_lufs = af.measure_loudness(audio)
    gain = af.ENGINE_GAIN_DB["google"]
    assert out_lufs - raw_lufs == pytest.approx(gain, abs=0.6), \
        f"เกนของ engine ไม่ได้ถูกใส่ในเส้นสำรอง: {raw_lufs} -> {out_lufs} (คาด {gain:+.1f} dB)"


def test_drill_การ_encode_ของเราเพิ่ม_padding_ครั้งเดียวไม่ใช่ต่อ_chunk(monkeypatch):
    """แยกให้ออกว่า padding มาจากใคร — ของเรา หรือของไฟล์ต้นทาง

    เส้น fallback ต่อ MP3 จาก Google หลายก้อนแล้ว encode รวมครั้งเดียว
    **ก้อนของ Google แต่ละก้อนมี encoder padding ของมันเองติดมาแล้ว** (~0.04 s/ก้อน)
    ⇒ ความยาวรวมจะเกินผลบวกของเสียงจริงอยู่แล้วโดยที่เรายังไม่ได้ทำอะไร

    เทสต์นี้จึงวัด **ส่วนที่ *เรา* เพิ่ม** = เทียบกับ bytes ที่ต่อกันแล้วแต่ยังไม่ผ่าน encode ของเรา
    ถ้าใครเปลี่ยนไป encode ทีละ chunk ตัวเลขนี้จะโตตามจำนวน chunk ทันที
    """
    text = "ข้อความมี merge PR " + "ประโยคไทยยาวพอควรสำหรับทดสอบ " * 30
    rec, payload = run_with_google(monkeypatch, engines.LocalF5Engine(), text=text)
    audio, _ = rec.files[0]
    chunks = len(bot._split_text(text))
    joined_raw = payload * chunks                     # เหมือนที่ bot ต่อ ก่อน encode
    added = af.probe(audio).seconds - af.count_samples(joined_raw) / af.SAMPLE_RATE
    assert added < 0.15, \
        (f"เรา เพิ่มความยาว {added:.3f}s ที่ {chunks} ก้อน — "
         f"เกินหนึ่ง padding (~{af.ENCODE_PADDING_SECONDS}s) แปลว่า encode ทีละ chunk")
    assert chunks >= 3, "ต้องมีหลายก้อนถึงจะแยกออกว่า padding สะสมหรือไม่"
