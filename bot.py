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
MAX_CHARS = 5000
PORT = int(os.environ.get("PORT", 8443))

TTS_URL = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"


async def _synthesize(text: str) -> bytes:
    async with httpx.AsyncClient() as client:
        resp = await client.post(TTS_URL, json={
            "input": {"text": text},
            "voice": {"languageCode": LANGUAGE_CODE, "name": VOICE_NAME},
            "audioConfig": {"audioEncoding": "MP3"},
        }, timeout=60)
        resp.raise_for_status()
        return base64.b64decode(resp.json()["audioContent"])


async def start(update: Update, context):
    await update.message.reply_text("ส่ง text มา แล้วจะแปลงเป็นเสียงให้ฟัง")


async def handle_text(update: Update, context):
    text = update.message.text.strip()
    if not text:
        return
    if len(text) > MAX_CHARS:
        await update.message.reply_text(f"ข้อความยาวเกิน {MAX_CHARS} ตัวอักษร")
        return

    msg = await update.message.reply_text("กำลังสร้างเสียง...")

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
