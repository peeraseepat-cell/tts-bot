import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import bot


class ParseAllowedChatIdsTests(unittest.TestCase):
    def test_empty_string_means_no_allowlist(self):
        self.assertEqual(bot._parse_allowed_chat_ids(""), frozenset())

    def test_parses_comma_separated_ids(self):
        self.assertEqual(
            bot._parse_allowed_chat_ids("123456789,-100987654321"),
            frozenset({123456789, -100987654321}),
        )

    def test_tolerates_spaces_around_commas(self):
        self.assertEqual(
            bot._parse_allowed_chat_ids(" 111 , 222 "),
            frozenset({111, 222}),
        )

    def test_invalid_id_fails_loudly(self):
        with self.assertRaises(ValueError):
            bot._parse_allowed_chat_ids("111,abc")


class IsAllowedChatTests(unittest.TestCase):
    def test_no_allowlist_allows_everyone(self):
        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset()):
            self.assertTrue(bot._is_allowed_chat(42))

    def test_listed_chat_is_allowed(self):
        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({42})):
            self.assertTrue(bot._is_allowed_chat(42))

    def test_unlisted_chat_is_blocked(self):
        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({42})):
            self.assertFalse(bot._is_allowed_chat(999))


def _fake_update(chat_id: int, text: str = "สวัสดี"):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(
            text=text,
            reply_text=mock.AsyncMock(),
        ),
    )


class HandlerGuardTests(unittest.TestCase):
    def test_handle_text_ignores_unauthorized_chat(self):
        update = _fake_update(chat_id=999)
        context = SimpleNamespace(bot=mock.AsyncMock(), application=mock.Mock())

        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({42})):
            asyncio.run(bot.handle_text(update, context))

        update.message.reply_text.assert_not_awaited()
        self.assertNotIn(999, bot.PENDING_BUFFERS)

    def test_handle_text_serves_authorized_chat(self):
        update = _fake_update(chat_id=42)
        reply_message = SimpleNamespace(message_id=1)
        update.message.reply_text = mock.AsyncMock(return_value=reply_message)
        fake_task = mock.Mock()
        fake_task.done.return_value = True
        context = SimpleNamespace(
            bot=mock.AsyncMock(),
            application=mock.Mock(create_task=mock.Mock(return_value=fake_task)),
        )

        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({42})):
            try:
                asyncio.run(bot.handle_text(update, context))
            finally:
                bot.PENDING_BUFFERS.pop(42, None)

        update.message.reply_text.assert_awaited()

    def test_status_ignores_unauthorized_chat(self):
        update = _fake_update(chat_id=999, text="/status")

        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({42})):
            asyncio.run(bot.status(update, None))

        update.message.reply_text.assert_not_awaited()

    def test_start_ignores_unauthorized_chat(self):
        update = _fake_update(chat_id=999, text="/start")

        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({42})):
            asyncio.run(bot.start(update, None))

        update.message.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
