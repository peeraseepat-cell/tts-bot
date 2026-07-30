"""เทสต์ format contract — decode ของจริงแล้วนับ sample ไม่เชื่อ header ไม่เชื่อชนิดข้อมูล

เทสต์ที่มีอยู่เดิมใช้ `FakeEngine` ทั้งหมด ⇒ มันตรวจ *control flow* ว่าเรียกถูกตัว
แต่ไม่เคยตรวจ *data contract* ว่าของที่ไหลออกมาหน้าตาถูกไหม (บทเรียน 2026-07-30)
ไฟล์นี้คือด่านของ data contract

**ทำไมไม่ commit ไฟล์เสียงจริงจาก F5 เป็น fixture**: output ของ F5 คือเสียงโคลนของพี่ชาย Boommer
= ข้อมูล biometric ซึ่งจงใจเก็บไว้นอก repo ⇒ เทสต์ประจำใช้สัญญาณสังเคราะห์
(ตรวจคณิตศาสตร์ของเกน/limiter/format ได้ครบ) ส่วนของจริงจาก 2 engine อยู่ใน
`test_real_engine_output` ที่ข้ามเองเมื่อไม่มี GPU/API key
"""
import math
import os
import struct
import subprocess
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_format as af


def make_wav(seconds: float = 1.0, amplitude: float = 0.5, freq: float = 220.0,
             rate: int = 24000) -> bytes:
    """คลื่นไซน์ — ความดังคำนวณล่วงหน้าได้ ⇒ ใช้ตรวจว่าเกนขยับจริงตามที่สั่ง"""
    frames = int(rate * seconds)
    buf = bytearray()
    for i in range(frames):
        value = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        buf += struct.pack("<h", value)
    import io
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(buf))
    return bio.getvalue()


# ---------- contract พื้นฐาน ----------

def test_encode_ตรงตาม_contract_ทุกช่อง():
    out = af.encode_sync(make_wav(1.0), 0.0)
    info = af.probe(out)
    assert info.codec == "mp3"
    assert info.sample_rate == 24000
    assert info.channels == 1
    assert info.matches_contract()


def test_decode_นับ_sample_ได้จริงไม่ใช่อ่านจาก_header():
    out = af.encode_sync(make_wav(2.0), 0.0)
    info = af.probe(out)
    # 2 วินาที @ 24 kHz = 48000 sample + padding ของ LAME (~0.06 s)
    assert 48000 <= info.samples <= 48000 + int(0.1 * af.SAMPLE_RATE)
    assert info.samples == af.count_samples(out)


def test_input_ที่ไม่ใช่เสียงต้องถูกปฏิเสธ_ไม่ใช่คืน_bytes_ว่าง():
    """ด่านต้องหยุดการกระทำได้จริง — ทดสอบด้วยของที่ควรถูกปฏิเสธ"""
    with pytest.raises(af.FormatError):
        af.encode_sync(b"this is not audio at all", 0.0)
    with pytest.raises(af.FormatError):
        af.probe(b"\x00" * 100)


def test_stereo_48k_ถูกบีบเข้า_contract():
    """input ที่ผิด contract ต้องออกมาถูก ไม่ใช่ผ่านไปเฉยๆ"""
    src = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=300:duration=1:sample_rate=48000",
         "-ac", "2", "-f", "wav", "pipe:1"],
        capture_output=True, check=True).stdout
    info = af.probe(af.encode_sync(src, 0.0))
    assert (info.sample_rate, info.channels) == (24000, 1)


# ---------- เกนคงที่ ----------

def test_เกนขยับความดังจริงตามที่สั่ง():
    quiet = make_wav(2.0, amplitude=0.05)
    base = af.measure_loudness(af.encode_sync(quiet, 0.0))
    boosted = af.measure_loudness(af.encode_sync(quiet, 6.0))
    assert boosted - base == pytest.approx(6.0, abs=0.5)


def test_ห้าม_auto_level_ของ_alimiter():
    """ด่านที่จับ regression อันตรายที่สุดของไฟล์นี้ — **ซ้อมด่านแล้วว่าปฏิเสธจริง**

    `alimiter` มี `level=true` เป็น default (auto level) ผลที่ **วัดได้จริง** คือมันดัน
    ความดังขึ้น **+1.0 dB เท่ากันทุกระดับ** (makeup = 1/limit) ไม่ใช่ "รีนอร์มไลซ์ให้ดังเต็ม"
    ⇒ เกนคงที่ทั้งชุดที่วัดมาจะเพี้ยนไป 1 dB **เงียบๆ**

    สำคัญ: เทสต์นี้ต้องวัด **ค่าสัมบูรณ์** ห้ามวัดผลต่างระหว่างสองสัญญาณ
    เพราะ auto level ขยับทั้งคู่เท่ากัน ⇒ เทสต์แบบผลต่างผ่านทั้งที่ contract พังแล้ว
    (ฉบับแรกของเทสต์นี้เขียนแบบผลต่าง — ซ้อมด่านแล้วพบว่ามันปล่อยผ่าน จึงเขียนใหม่)
    """
    measured = af.measure_loudness(af.encode_sync(make_wav(2.0, amplitude=0.5), 0.0))
    assert measured == pytest.approx(-10.4, abs=0.4), (
        f"ความดังของสัญญาณอ้างอิงเปลี่ยนไปเป็น {measured} LUFS — "
        "อาจมีคนลบ level=disabled ออก หรือแก้ห่วงโซ่ filter"
    )


def test_limiter_กันคลิปเมื่อสัญญาณเกินเต็มสเกล():
    """F5 ของจริงมี true peak +0.2 dBFS ⇒ เคสนี้ไม่ใช่สมมติ"""
    out = af.encode_sync(make_wav(1.0, amplitude=0.99), 12.0)
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", "pipe:0",
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        input=out, capture_output=True)
    peaks = [float(line.split()[1]) for line in proc.stderr.decode().splitlines()
             if line.strip().startswith("Peak:")]
    assert peaks and max(peaks) <= af.LIMIT_DBTP + 0.5, f"peak ทะลุ limiter: {peaks}"


def test_เกนของสอง_engine_ทำให้ความดังมาบรรจบกัน():
    """หัวใจของ contract: สลับ engine กลางบทความแล้วความดังต้องไม่กระโดด

    จำลองด้วยสัญญาณที่ดังต่างกัน 3.0 LU เท่าที่วัดได้จริงระหว่าง F5 กับ Google
    """
    louder = make_wav(2.0, amplitude=0.30)              # แทน F5
    quieter = make_wav(2.0, amplitude=0.30 * 10 ** (-3.0 / 20))   # แทน Google (เบากว่า 3 dB)
    before = af.measure_loudness(af.encode_sync(louder, 0.0)) - \
        af.measure_loudness(af.encode_sync(quieter, 0.0))
    after = af.measure_loudness(af.encode_sync(louder, af.ENGINE_GAIN_DB["local-f5"])) - \
        af.measure_loudness(af.encode_sync(quieter, af.ENGINE_GAIN_DB["google"]))
    assert abs(before) == pytest.approx(3.0, abs=0.5)   # ตั้งต้นต่างกันจริง
    assert abs(after) < 0.5, f"เกนคงที่ไม่ปิดช่องว่าง: {after:.2f} LU"


def test_ค่าคงที่ที่วัดไว้ยังตรงกับที่ประกาศ():
    """กันตัวเลขในเอกสารกับตัวเลขในโค้ดเดินคนละทาง"""
    gap = abs(af.MEASURED_LUFS["local-f5"] - af.MEASURED_LUFS["google"])
    assert gap == pytest.approx(af.MEASURED_SPREAD_LU, abs=0.01)
    for name, raw_lufs in (("local-f5", -15.75), ("google", -18.72)):
        expected = af.NOMINAL_TARGET_LUFS - raw_lufs
        assert af.ENGINE_GAIN_DB[name] == pytest.approx(expected, abs=0.05)


# ---------- เส้นทางจริงของ engine (ไม่ใช่ FakeEngine) ----------

def test_google_engine_บีบ_output_เข้า_contract():
    """เส้นทางสำเร็จของ GoogleEngine ไม่เคยมีเทสต์มาก่อน — encode ที่เพิ่งเสียบจึงไม่มีอะไรคุ้ม

    จำลอง transport ให้คืน **เสียงจริง** (ไม่ใช่ b"fake") ที่ผิด contract ตั้งใจ:
    48 kHz stereo ⇒ ถ้า engine ไม่ได้บีบเข้า contract เทสต์นี้จะจับได้
    """
    import asyncio
    import base64
    import json as _json

    import httpx

    import engines

    src = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=400:duration=1:sample_rate=48000",
         "-ac", "2", "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
        capture_output=True, check=True).stdout

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"audioContent": base64.b64encode(src).decode()})

    engine = engines.GoogleEngine("SECRET-KEY-123", "th-TH-Chirp3-HD-Achernar")

    async def run():
        engine._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                           headers=engine._headers)
        try:
            return await engine.synth("ทดสอบ", engines.VoiceSpec(agent="a", engine="google"))
        finally:
            await engine.aclose()

    out = asyncio.run(run())
    info = af.probe(out)
    assert info.matches_contract(), f"Google output ไม่เข้า contract: {info}"
    assert info.samples > 0

    # หมายเหตุจาก review ของ AgentA: key ต้องไม่อยู่ใน URL เพราะ httpx log URL ที่ระดับ INFO
    assert "SECRET-KEY-123" not in captured["url"], "API key หลุดอยู่ใน URL — จะไหลลง log"
    assert captured["headers"].get("x-goog-api-key") == "SECRET-KEY-123"


def test_google_engine_ใช้_client_ตัวเดิมซ้ำ():
    """เปิด AsyncClient ใหม่ทุก chunk เสีย handshake +3% ต่อ chunk (วัดแล้ว)"""
    import asyncio

    import engines

    engine = engines.GoogleEngine("k", "v")

    async def run():
        first = await engine._get_client()
        second = await engine._get_client()
        try:
            return first is second
        finally:
            await engine.aclose()

    assert asyncio.run(run())


# ---------- ของจริงจาก 2 engine ----------

@pytest.mark.skipif(not os.environ.get("TTS_REAL_ENGINE"),
                    reason="ต้องใช้ GPU + Google API key — ตั้ง TTS_REAL_ENGINE=1 เพื่อรัน")
def test_real_engine_output():
    """output จริงจากทั้งสอง engine → decode → นับ sample → ตรวจว่าความดังมาบรรจบ

    ไม่รันในชุดประจำเพราะต้องโหลดโมเดล (VRAM) และยิง API จริง (เสียเงิน)
    """
    import asyncio

    import engines

    ref_wav = os.environ.get("TTS_REF_WAV", "/home/boom/from-AgentA/voice-ref/cand-34.50.wav")
    ref_txt = os.environ.get("TTS_REF_TXT", "/home/boom/from-AgentA/voice-ref/cand-34.50.txt")
    api_key = os.environ["GOOGLE_API_KEY"]
    text = "ทดสอบสัญญารูปแบบเสียงของทั้งสองเครื่อง"

    async def run():
        local = engines.LocalF5Engine()
        google = engines.GoogleEngine(api_key, "th-TH-Chirp3-HD-Achernar")
        try:
            a = await local.synth(text, engines.VoiceSpec(
                agent="boommer", engine="local", ref_wav=ref_wav,
                ref_text=open(ref_txt).read().strip()))
            b = await google.synth(text, engines.VoiceSpec(
                agent="agent", engine="google", google_voice="th-TH-Chirp3-HD-Achernar"))
            return a, b
        finally:
            await google.aclose()

    local_out, google_out = asyncio.run(run())
    for name, data in (("local-f5", local_out), ("google", google_out)):
        info = af.probe(data)
        assert info.matches_contract(), f"{name} ผิด contract: {info}"
        assert info.samples > af.SAMPLE_RATE * 0.5, f"{name} สั้นผิดปกติ: {info.samples} sample"
    gap = abs(af.measure_loudness(local_out) - af.measure_loudness(google_out))
    assert gap < 1.5, f"ความดังสอง engine ต่างกัน {gap:.2f} LU — เกนคงที่ไม่ทำงานกับของจริง"
