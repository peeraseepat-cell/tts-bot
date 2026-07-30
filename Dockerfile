FROM python:3.12-slim

# ffmpeg เป็น **runtime dependency ของทั้งสองเส้นทาง** ไม่ใช่ของเฉพาะ local engine
# `audio_format.py` เรียก ffmpeg/ffprobe ตรงๆ เพื่อบังคับ format contract
# (MP3 24 kHz mono + เกนคงที่ต่อ engine + limiter) ⇒ ไม่มี = ทุก synth ล้ม FileNotFoundError
# แม้ deploy แบบ Google-only ที่ไม่ได้ลง requirements-local.txt ก็ยังต้องมี
#
# ⚠️ ตัวเลขความดังทั้งชุดวัดบน **ffmpeg 6.1.1 (Ubuntu, pc-office)** ส่วน image นี้เป็น
# Debian bookworm ที่ให้ **ffmpeg 5.1.x** — ยังไม่ได้ยืนยันว่า filter chain ทำงานเหมือนกัน
# เพราะ pc-office **ไม่มี docker** จึง build ไม่ได้ ⇒ ข้อนี้ยัง INCONCLUSIVE ไม่ใช่ "ผ่าน"
# ด่านที่กันไว้แทน: `audio_format.preflight()` ล้มดังตอน startup ถ้า encode ไม่ตรง contract
#
# วิธี verify เมื่ออยู่บนเครื่องที่มี docker:
#   docker build -t tts-bot .
#   docker run --rm tts-bot ffmpeg -version
#   docker run --rm tts-bot python -c "import audio_format; print(audio_format.preflight())"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
