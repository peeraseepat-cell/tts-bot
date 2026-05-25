import base64
import io
import os

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
VOICE_NAME = os.environ.get("TTS_VOICE", "th-TH-Chirp3-HD-Achernar")
LANGUAGE_CODE = VOICE_NAME.rsplit("-Chirp3", 1)[0]
MAX_CHARS = 20000
CHUNK_SIZE = 1500
MAX_SENTENCE = 250
PORT = int(os.environ.get("PORT", 8443))

TTS_URL = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"


def _break_long_sentences(text: str) -> str:
    result = []
    since_punct = 0
    for char in text:
        result.append(char)
        since_punct += 1
        if char in ".!?\n":
            since_punct = 0
        elif since_punct >= MAX_SENTENCE and char == " ":
            result.append(".")
            since_punct = 0
    return "".join(result)


def _split_text(text: str) -> list[str]:
    text = _break_long_sentences(text)
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks = []
    while text:
        if len(text) <= CHUNK_SIZE:
            chunks.append(text)
            break
        cut = CHUNK_SIZE
        for sep in ["\n", ". ", "! ", "? ", ", ", " "]:
            pos = text.rfind(sep, 0, CHUNK_SIZE)
            if pos > CHUNK_SIZE // 2:
                cut = pos + len(sep)
                break
        chunks.append(text[:cut])
        text = text[cut:]
    return chunks


async def _synthesize(text: str) -> bytes:
    chunks = _split_text(text)
    parts = []
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            resp = await client.post(TTS_URL, json={
                "input": {"text": chunk},
                "voice": {"languageCode": LANGUAGE_CODE, "name": VOICE_NAME},
                "audioConfig": {"audioEncoding": "MP3"},
            }, timeout=60)
            if resp.status_code != 200:
                err = resp.json().get("error", {}).get("message", resp.text[:200])
                raise RuntimeError(f"Google TTS: {err}")
            parts.append(base64.b64decode(resp.json()["audioContent"]))
    return b"".join(parts)


async def start(update: Update, context):
    await update.message.reply_text("ส่ง text มา แล้วจะแปลงเป็นเสียงให้ฟัง")


async def handle_text(update: Update, context):
    text = update.message.text.strip()
    if not text:
        return
    if len(text) > MAX_CHARS:
        await update.message.reply_text(f"ข้อความยาวเกิน {MAX_CHARS} ตัวอักษร")
        return

    n_chunks = len(_split_text(text))
    status = "กำลังสร้างเสียง..." if n_chunks == 1 else f"กำลังสร้างเสียง ({n_chunks} ท่อน)..."
    msg = await update.message.reply_text(status)

    try:
        audio_bytes = await _synthesize(text)
        await update.message.reply_voice(voice=io.BytesIO(audio_bytes))
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"เกิดข้อผิดพลาด: {e}")


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
