import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import bot


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

    def test_tts_timeout_defaults_fail_fast_enough_for_telegram(self):
        self.assertEqual(bot.TTS_PART_MAX_CHUNKS, 4)
        self.assertEqual(bot.TTS_MAX_RETRIES, 0)
        self.assertEqual(bot.TTS_READ_TIMEOUT, 15)
        self.assertEqual(bot.TTS_FILE_TIMEOUT, 30)


class SynthesizeProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesize_part_reports_chunk_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            meter = bot.UsageMeter(
                path=os.path.join(temp_dir, "usage.json"),
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


class UsageMeterTests(unittest.TestCase):
    def test_records_monthly_usage_and_reset_countdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            meter = bot.UsageMeter(
                path=os.path.join(temp_dir, "usage.json"),
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

    def test_resets_when_month_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            meter = bot.UsageMeter(
                path=os.path.join(temp_dir, "usage.json"),
                limit=1_000_000,
                timezone_name="Asia/Bangkok",
            )
            meter.record(50_000, now=datetime(2026, 5, 31, 23, 0, tzinfo=ZoneInfo("Asia/Bangkok")))

            summary = meter.record(10, now=datetime(2026, 6, 1, 0, 1, tzinfo=ZoneInfo("Asia/Bangkok")))

            self.assertEqual(summary.period, "2026-06")
            self.assertEqual(summary.used, 10)
            self.assertEqual(summary.remaining, 999_990)


if __name__ == "__main__":
    unittest.main()
