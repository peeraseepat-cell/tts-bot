"""ซ้อมด่าน 5 ข้อของ design §6 — ด้วยของที่ **ควรถูกปฏิเสธ**

`check && do` ไม่ใช่ `check ; do` — fallback ที่ไม่เคยถูกซ้อมคือป้าย ไม่ใช่ประตู
ทุกเทสต์ในไฟล์นี้ยิงด้วยเคสที่ต้องล้ม แล้วตรวจว่า **ล้มถูกทาง** ไม่ใช่ตรวจว่าเคสดีผ่าน
"""
import asyncio
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import engines  # noqa: E402
import thai_text  # noqa: E402


def voice(**kw):
    base = dict(agent="Boommer", engine="local",
                ref_wav="/nonexistent/ref.wav", ref_text="อ้างอิง")
    base.update(kw)
    return engines.VoiceSpec(**base)


# ---------- ด่าน 1: ละตินตกค้าง ตกทั้ง job ----------

def test_ด่าน1_ละตินตกค้างตีตกทั้ง_job_ไม่ใช่แค่ท่อนนั้น():
    engine = engines.LocalF5Engine()
    text = "ย่อหน้าแรกภาษาไทยล้วน\nย่อหน้าสองมีคำว่า merge PR ปนอยู่\nย่อหน้าสามไทยล้วน"
    with pytest.raises(engines.EngineUnavailable) as exc:
        asyncio.run(engine.synth_long(text, voice(ref_wav=__file__)))
    assert "translit" in str(exc.value)


def test_ด่าน1_เลขอารบิกก็ตีตก():
    engine = engines.LocalF5Engine()
    with pytest.raises(engines.EngineUnavailable):
        asyncio.run(engine.synth_long("ราคา 250 บาท ค่ะ", voice(ref_wav=__file__)))


def test_ไทยล้วนไม่ถูกด่านตีตก():
    """ด่านที่ปฏิเสธทุกอย่างก็ไร้ประโยชน์พอกับด่านที่ปล่อยทุกอย่าง"""
    engines.LocalF5Engine.check_text("ประโยคนี้เป็นภาษาไทยล้วนไม่มีอะไรตกค้าง")


# ---------- ด่าน 2: ref หายจากดิสก์ ----------

def test_ด่าน2_ref_หายจากดิสก์ตกไป_fallback_ไม่ใช่_crash():
    engine = engines.LocalF5Engine()
    with pytest.raises(engines.EngineUnavailable) as exc:
        asyncio.run(engine.synth_long("ข้อความไทยล้วน", voice()))
    assert "ref audio หาย" in str(exc.value)


def test_ด่าน2_voice_ที่ตั้งเป็น_google_ไม่ถูกส่งเข้า_local():
    engine = engines.LocalF5Engine()
    with pytest.raises(engines.EngineUnavailable):
        asyncio.run(engine.synth_long("ข้อความไทย", voice(engine="google")))


# ---------- ด่าน 3: โหลดโมเดลไม่ได้ = EngineUnavailable ไม่ใช่ hang ----------

def test_ด่าน3_โหลดโมเดลไม่ได้คืน_EngineUnavailable(monkeypatch):
    engine = engines.LocalF5Engine()
    engine._load_failed = "OutOfMemoryError: CUDA out of memory"
    with pytest.raises(engines.EngineUnavailable) as exc:
        asyncio.run(engine.synth_long("ข้อความไทยล้วน", voice(ref_wav=__file__)))
    assert "โหลดโมเดลไม่สำเร็จ" in str(exc.value)


# ---------- ด่าน 4: ทางถอน — ปิดสนิทได้ ไม่มี half-door ----------

def test_ด่าน4_ปิด_USE_LOCAL_TTS_แล้วไม่แตะ_local_เลย(monkeypatch):
    """สำคัญกว่าที่เห็น: ถ้าปิดแล้วยัง import engines/torch อยู่ = ยังไม่ได้ปิดจริง"""
    import bot

    monkeypatch.setattr(bot, "USE_LOCAL_TTS", False)

    called = {"local": False}

    async def boom(*a, **k):
        called["local"] = True
        return True

    monkeypatch.setattr(bot, "_process_job_local", boom)

    async def fake_synth(*a, **k):
        raise RuntimeError("หยุดตรงนี้พอ — แค่พิสูจน์ว่าไปถึงเส้น Google")

    monkeypatch.setattr(bot, "_synthesize_part_with_timeout", fake_synth)

    job = bot.TTSJob(chat_id=1, status_message_id=2, text="ข้อความ",
                     parts=["ข้อความ"], queued_at=bot.datetime.now())

    class FakeBot:
        async def edit_message_text(self, **k):
            return None

        async def send_message(self, **k):
            return None

    class FakeApp:
        bot = FakeBot()

    asyncio.run(bot._process_job(FakeApp(), job))
    assert called["local"] is False, "ปิดแล้วยังเรียกเส้น local = half-door"


# ---------- ด่าน 5: ทั้งสองเส้นล้ม ต้องดัง ไม่เงียบ ----------

def test_ด่าน5_local_ล้มแล้วตกไป_google_ที่ล้มด้วย_ผู้ใช้ต้องได้ข้อความ(monkeypatch):
    import bot

    monkeypatch.setattr(bot, "USE_LOCAL_TTS", True)

    async def local_declines(app, job):
        return False            # engine รับไม่ได้ ⇒ ตกไป Google

    async def google_fails(*a, **k):
        raise RuntimeError("Google TTS: quota exceeded")

    monkeypatch.setattr(bot, "_process_job_local", local_declines)
    monkeypatch.setattr(bot, "_synthesize_part_with_timeout", google_fails)

    seen = []

    class FakeBot:
        async def edit_message_text(self, **k):
            seen.append(k.get("text", ""))

        async def send_message(self, **k):
            seen.append(k.get("text", ""))

    class FakeApp:
        bot = FakeBot()

    job = bot.TTSJob(chat_id=1, status_message_id=2, text="ข้อความ",
                     parts=["ข้อความ"], queued_at=bot.datetime.now())
    asyncio.run(bot._process_job(FakeApp(), job))
    assert any("ข้อผิดพลาด" in s for s in seen), f"ล้มเงียบ — ผู้ใช้ไม่ได้รับอะไร: {seen}"
    assert any("quota exceeded" in s for s in seen), "กลืนสาเหตุจริงไป"


# ---------- ชั้นหั่นข้อความ ----------

def test_ทุกก้อนไม่เกิน_MAX_และไม่เกินที่ส่งให้_lib():
    text = "\n".join("ประโยคยาวพอสมควรสำหรับทดสอบการหั่นข้อความให้เป็นก้อนเล็ก " * 3
                     for _ in range(5))
    items, stat = thai_text.plan(text)
    assert stat["max_chunk"] <= thai_text.MAX_CHUNK
    assert thai_text.MAX_CHUNK < thai_text.LIB_MAX_CHARS, "lib จะหั่นก้อนเราซ้ำ"
    assert all(len(c) <= thai_text.MAX_CHUNK for c, _ in items)


def test_ก้อนสุดท้ายไม่มีความเงียบต่อท้าย():
    items, _ = thai_text.plan("ย่อหน้าหนึ่ง\nย่อหน้าสอง")
    assert items[-1][1] == 0.0


def test_ตัดกลางคำถูกนับไว้รายงานไม่ใช่เงียบ():
    items, stat = thai_text.plan("ก" * 250)
    assert stat["hard_cuts"] == 2, f"ตัดดิบแล้วไม่ได้นับ: {stat}"


def test_ข้อความว่างไม่ระเบิด():
    items, stat = thai_text.plan("   \n  \n")
    assert items == [] and stat["chunks"] == 0


@pytest.mark.skipif(not os.path.exists(
    "/home/boom/Code/github.com/peeraseepat-cell/AgentP-oracle/ψ/lab/chunk_th.py"),
    reason="ไม่มีต้นฉบับฝั่ง oracle บนเครื่องนี้")
def test_สำเนาที่สองยังไม่_drift_จากต้นฉบับ():
    """หนี้ 'สำเนาที่สอง' ยังไม่ถูกปิด — อย่างน้อยทำให้ drift **ตรวจจับได้**

    ไม่ได้แก้หนี้ แค่ทำให้มันส่งเสียงตอนแตกต่าง (3 ทางเลือกยังรอเคาะ)
    """
    sys.path.insert(0, "/home/boom/Code/github.com/peeraseepat-cell/AgentP-oracle/ψ/lab")
    import chunk_th

    text = "ย่อหน้าแรกยาวพอควรสำหรับทดสอบการหั่น\nย่อหน้าที่สองก็ยาวพอกัน มีช่องว่างหลายจุด ให้ตัดได้"
    mine, mine_stat = thai_text.plan(text, thai_text.MAX_CHUNK)
    theirs, theirs_stat = chunk_th.plan(text, chunk_th.MAX)
    assert mine == theirs, "สำเนาที่สองเริ่ม drift จากต้นฉบับแล้ว"
    assert mine_stat == theirs_stat


def test_เส้นคั่นและก้อนที่ไม่มีอะไรให้อ่านถูกคัดออกและถูกนับ():
    """เจอจากบทความจริงของ Boommer — เส้นคั่น Markdown ทำให้ทั้ง job ระเบิด

    โมเดลคืนเสียงเปล่าให้ก้อน '--------------------' แล้ว np.concatenate ล้มด้วย
    "array at index 170 has 0 dimension(s)" ซึ่งอ่านไม่ออกเลยว่าต้นเหตุคืออะไร
    """
    items, stat = thai_text.plan("ประโยคแรก\n--------------------\nประโยคที่สอง\n***\nประโยคที่สาม")
    assert stat["dropped_unspeakable"] == 2
    assert stat["chunks"] == 3
    assert all(thai_text.is_speakable(c) for c, _ in items)


@pytest.mark.skipif(importlib.util.find_spec("numpy") is None,
                    reason="ต้องมี dependency ชุด local — venv แบบ Google-only ข้ามข้อนี้")
def test_ก้อนที่มีเนื้อแต่ได้เสียงเปล่าต้องล้มดังไม่ใช่ข้ามเงียบ(monkeypatch):
    """ข้ามก้อนเงียบๆ = คำหายโดยไม่มีใครรู้ — ตระกูลเดียวกับบั๊กที่ไล่มาทั้งวัน

    เทสต์นี้ข้ามเองบนเครื่องที่ไม่มี numpy เพราะ engine จะล้มที่ด่าน dependency ก่อน
    ซึ่ง **ถูกต้องแล้ว** — เป็นคนละด่านกัน ไม่ใช่ด่านนี้ล้มเหลว
    """
    import numpy as np

    engine = engines.LocalF5Engine()

    class FakeTTS:
        def infer(self, **kw):
            return np.array([])          # เสียงเปล่าจากก้อนที่มีเนื้อ

    monkeypatch.setattr(engine, "_ensure_model", lambda: FakeTTS())
    with pytest.raises(engines.EngineUnavailable) as exc:
        asyncio.run(engine.synth_long("ประโยคไทยที่มีเนื้อหาให้อ่าน", voice(ref_wav=__file__)))
    assert "เสียงเปล่า" in str(exc.value)
