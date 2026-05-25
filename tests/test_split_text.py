import os
import unittest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import bot


class SplitTextTests(unittest.TestCase):
    def test_long_thai_without_spaces_gets_sentence_boundaries(self):
        text = "ในฐาน" + ("ภาษาไทยไม่มีเว้นวรรค" * 80)

        chunks = bot._split_text(text)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(
            all(chunk.rstrip().endswith((".", "!", "?", "。", "…")) for chunk in chunks),
            chunks[:3],
        )
        self.assertLessEqual(max(len(chunk) for chunk in chunks), bot.CHUNK_SIZE + 1)
        self.assertEqual("".join(chunk[:-1] for chunk in chunks), text)


if __name__ == "__main__":
    unittest.main()
