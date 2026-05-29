import asyncio
import io
import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import bot


class FakeSupabaseResponse:
    def __init__(self, data):
        self.data = data


class FakeSupabaseClient:
    def __init__(self):
        self.rows = {}
        self.fail_reads = False
        self.fail_writes = False
        self.upserts = []

    def table(self, table_name):
        return FakeSupabaseTable(self, table_name)


class FakeSupabaseTable:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.operation = None
        self.period = None
        self.row = None
        self.on_conflict = None

    def select(self, _columns):
        self.operation = "select"
        return self

    def eq(self, _column, value):
        self.period = value
        return self

    def limit(self, _count):
        return self

    def upsert(self, row, on_conflict=None):
        self.operation = "upsert"
        self.row = row
        self.on_conflict = on_conflict
        return self

    def execute(self):
        if self.operation == "select":
            if self.client.fail_reads:
                raise RuntimeError("supabase read failed")
            row = self.client.rows.get(self.period)
            return FakeSupabaseResponse([row] if row else [])
        if self.operation == "upsert":
            if self.client.fail_writes:
                raise RuntimeError("supabase write failed")
            self.client.upserts.append((self.table_name, self.row, self.on_conflict))
            self.client.rows[self.row["period"]] = dict(self.row)
            return FakeSupabaseResponse([self.row])
        raise AssertionError("unexpected fake supabase operation")


class OutputPartTests(unittest.TestCase):
    def test_long_text_is_split_into_voice_file_parts(self):
        text = "ในฐาน" + ("ภาษาไทยไม่มีเว้นวรรค" * 500)

        parts = bot._split_output_parts(text)

        self.assertGreater(len(parts), 1)
        self.assertLessEqual(max(len(part) for part in parts), bot.PART_SIZE + 1)
        self.assertTrue(all(part.endswith(bot.SENTENCE_ENDINGS) for part in parts))
        self.assertTrue(all(len(bot._split_text(part)) <= bot.TTS_PART_MAX_CHUNKS for part in parts))

    def test_collect_window_default_is_five_seconds(self):
        self.assertEqual(bot.COLLECT_WINDOW_SECONDS, 5)

    def test_tts_timeout_defaults_support_long_voice_uploads(self):
        self.assertEqual(bot.TTS_PART_MAX_CHUNKS, 12)
        self.assertEqual(bot.TTS_MAX_RETRIES, 0)
        self.assertEqual(bot.TTS_READ_TIMEOUT, 15)
        self.assertEqual(bot.TTS_FILE_TIMEOUT, 60)
        self.assertEqual(bot.TELEGRAM_SEND_TIMEOUT, 90)

    def test_runtime_status_shows_timeout_config(self):
        summary = bot.USAGE_METER.preview()

        text = bot._format_runtime_status(summary)

        self.assertIn("part max chunks: 12", text)
        self.assertIn("TTS file timeout: 60s", text)
        self.assertIn("Telegram send timeout: 90s", text)


class SynthesizeProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesize_part_reports_chunk_progress(self):
        meter = bot.UsageMeter(
            client=FakeSupabaseClient(),
            limit=1_000_000,
            timezone_name="Asia/Bangkok",
        )
        progress = []

        async def fake_post_tts_chunk(_client, chunk):
            return f"audio:{len(chunk)}".encode()

        async def on_progress(done, total):
            progress.append((done, total))

        with mock.patch.object(bot, "USAGE_METER", meter), mock.patch.object(
            bot, "_post_tts_chunk", fake_post_tts_chunk
        ):
            audio, used_chars, requests, _summary = await bot._synthesize_part(
                "ภาษาไทยไม่มีเว้นวรรค" * 40,
                progress=on_progress,
            )

        self.assertGreater(requests, 1)
        self.assertEqual(progress[0], (1, requests))
        self.assertEqual(progress[-1], (requests, requests))
        self.assertGreater(len(audio), 0)
        self.assertEqual(used_chars, sum(len(chunk) for chunk in bot._split_text("ภาษาไทยไม่มีเว้นวรรค" * 40)))

    async def test_synthesize_part_with_timeout_fails_after_file_timeout(self):
        async def slow_synthesize_part(_text, progress=None):
            await asyncio.sleep(1)
            return b"", 0, 0, bot.USAGE_METER.preview()

        with mock.patch.object(bot, "TTS_FILE_TIMEOUT", 0.01), mock.patch.object(
            bot, "_synthesize_part", slow_synthesize_part
        ):
            with self.assertRaisesRegex(RuntimeError, "เกิน 0.01 วิ"):
                await bot._synthesize_part_with_timeout("ภาษาไทย")


class TelegramSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_voice_with_timeout_fails_after_telegram_timeout(self):
        class SlowBot:
            async def send_voice(self, **_kwargs):
                await asyncio.sleep(10)

        class FakeApp:
            bot = SlowBot()

        with mock.patch.object(bot, "TELEGRAM_SEND_TIMEOUT", 0.01):
            with self.assertRaisesRegex(RuntimeError, "Telegram เกิน 0.01 วิ"):
                await bot._send_voice_with_timeout(
                    app=FakeApp(),
                    chat_id=1,
                    voice=io.BytesIO(b"audio"),
                    caption="ไฟล์ 1/1",
                )

    async def test_process_job_marks_telegram_send_phase(self):
        class FakeBot:
            def __init__(self):
                self.edits = []
                self.voices = []

            async def edit_message_text(self, chat_id, message_id, text):
                self.edits.append(text)

            async def send_message(self, chat_id, text):
                self.edits.append(text)

            async def send_voice(self, chat_id, voice, caption, **_kwargs):
                self.voices.append((voice.getvalue(), caption))

        class FakeApp:
            def __init__(self):
                self.bot = FakeBot()

        app = FakeApp()

        async def fake_synthesize_part_with_timeout(_part, progress=None):
            if progress:
                await progress(1, 1)
            return b"audio", 5, 1, bot.USAGE_METER.preview()

        job = bot.TTSJob(
            chat_id=1,
            status_message_id=2,
            text="hello",
            parts=["hello"],
            queued_at=datetime.now(ZoneInfo("Asia/Bangkok")),
        )

        with mock.patch.object(bot, "_synthesize_part_with_timeout", fake_synthesize_part_with_timeout):
            await bot._process_job(app, job)
            await asyncio.sleep(0.05)

        self.assertIn("สร้างเสียงไฟล์ 1/1 เสร็จแล้ว กำลังส่งเข้า Telegram...", app.bot.edits)
        self.assertEqual(app.bot.voices[0], (b"audio", "ไฟล์ 1/1 · 5 ตัวอักษร"))


class UsageMeterTests(unittest.TestCase):
    def test_records_monthly_usage_and_reset_countdown(self):
        client = FakeSupabaseClient()
        meter = bot.UsageMeter(
            client=client,
            limit=1_000_000,
            timezone_name="Asia/Bangkok",
        )
        now = datetime(2026, 5, 25, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))

        summary = meter.record(12_345, now=now)

        self.assertEqual(summary.period, "2026-05")
        self.assertEqual(summary.used, 12_345)
        self.assertEqual(summary.remaining, 987_655)
        self.assertEqual(summary.reset_at.strftime("%Y-%m-%d %H:%M"), "2026-06-01 00:00")
        self.assertEqual(summary.days_until_reset, 7)
        self.assertEqual(client.rows["2026-05"]["used"], 12_345)
        self.assertEqual(client.rows["2026-05"]["limit"], 1_000_000)
        self.assertEqual(client.upserts[0][0], "tts_usage")
        self.assertEqual(client.upserts[0][2], "period")

    def test_resets_when_month_changes(self):
        client = FakeSupabaseClient()
        meter = bot.UsageMeter(
            client=client,
            limit=1_000_000,
            timezone_name="Asia/Bangkok",
        )
        meter.record(50_000, now=datetime(2026, 5, 31, 23, 0, tzinfo=ZoneInfo("Asia/Bangkok")))

        summary = meter.record(10, now=datetime(2026, 6, 1, 0, 1, tzinfo=ZoneInfo("Asia/Bangkok")))

        self.assertEqual(summary.period, "2026-06")
        self.assertEqual(summary.used, 10)
        self.assertEqual(summary.remaining, 999_990)
        self.assertEqual(client.rows["2026-05"]["used"], 50_000)
        self.assertEqual(client.rows["2026-06"]["used"], 10)

    def test_supabase_failure_logs_warning_and_uses_in_memory_fallback(self):
        client = FakeSupabaseClient()
        client.fail_reads = True
        client.fail_writes = True
        meter = bot.UsageMeter(
            client=client,
            limit=1_000_000,
            timezone_name="Asia/Bangkok",
        )
        now = datetime(2026, 5, 25, 20, 0, tzinfo=ZoneInfo("Asia/Bangkok"))

        with self.assertLogs(bot.LOGGER, level="WARNING") as logs:
            summary = meter.record(200, now=now)

        self.assertEqual(summary.used, 200)
        self.assertIn("Supabase usage", "\n".join(logs.output))
        self.assertEqual(meter.preview(now=now).used, 200)


if __name__ == "__main__":
    unittest.main()
