import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

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
