import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import httpx

import bot
from tests.test_gemini import _gemini_ok, _wav

BOT_ID = 999


class FakeTelegramBot:
    def __init__(self, pinned=None, fail_get_chat=False, fail_edit=False):
        self.id = BOT_ID
        self.pinned = pinned
        self.fail_get_chat = fail_get_chat
        self.fail_edit = fail_edit
        self.sent, self.pins, self.edits = [], [], []
        self._next_id = 100

    async def get_chat(self, chat_id):
        if self.fail_get_chat:
            raise RuntimeError("network")
        return SimpleNamespace(pinned_message=self.pinned)

    async def send_message(self, chat_id, text, **kw):
        self._next_id += 1
        self.sent.append((text, kw))
        return SimpleNamespace(message_id=self._next_id)

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.pins.append((message_id, kw))

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        if self.fail_edit:
            raise RuntimeError("message to edit not found")
        self.edits.append((message_id, text))


def _pinned(text, author_id=BOT_ID, message_id=55):
    return SimpleNamespace(text=text, message_id=message_id, from_user=SimpleNamespace(id=author_id))


def _reset():
    bot.CHAT_PERSONA.clear()
    bot.SETTINGS_MESSAGE_ID.clear()


class PersonaCatalogTests(unittest.TestCase):
    def test_four_personas_from_playground(self):
        self.assertEqual(
            [(p.key, p.name, p.voice) for p in bot.PERSONAS],
            [("jan", "น้องแจน", "Sami"), ("tom", "คุณทอม", "Achird"),
             ("leng", "ป้าเล้ง", "Kore"), ("dak", "พี่แด๊ก", "Algenib")],
        )
        self.assertTrue(all(p.style.startswith("Style: Professional") for p in bot.PERSONAS))
        self.assertEqual(bot.DEFAULT_PERSONA, "jan")

    def test_resolve_random_picks_a_concrete_persona(self):
        self.assertEqual(bot._resolve_persona("random", choice=lambda keys: keys[2]), "leng")
        self.assertEqual(bot._resolve_persona("tom"), "tom")
        self.assertEqual(bot._resolve_persona("nope"), bot.DEFAULT_PERSONA)


class KeyboardTests(unittest.TestCase):
    def test_keyboard_has_every_persona_plus_random_and_marks_current(self):
        markup = bot._settings_keyboard("leng")
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertEqual([b.callback_data for b in buttons],
                         ["persona:jan", "persona:tom", "persona:leng", "persona:dak", "persona:random"])
        marked = [b.text for b in buttons if "✓" in b.text]
        self.assertEqual(len(marked), 1)
        self.assertIn("ป้าเล้ง", marked[0])


class PinnedParseTests(unittest.TestCase):
    def test_accepts_own_tagged_message(self):
        self.assertEqual(bot._parse_pinned_persona(_pinned(bot._persona_tag("dak")), BOT_ID), "dak")

    def test_random_is_a_valid_saved_choice(self):
        self.assertEqual(bot._parse_pinned_persona(_pinned(bot._persona_tag("random")), BOT_ID), "random")

    def test_rejects_foreign_author_unknown_key_and_none(self):
        self.assertIsNone(bot._parse_pinned_persona(_pinned(bot._persona_tag("dak"), author_id=1), BOT_ID))
        self.assertIsNone(bot._parse_pinned_persona(_pinned("⚙️ x\n#persona=bogus"), BOT_ID))
        self.assertIsNone(bot._parse_pinned_persona(None, BOT_ID))
        self.assertIsNone(bot._parse_pinned_persona(_pinned(None), BOT_ID))


class ChatPersonaStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset()

    async def test_reads_pinned_once_then_caches(self):
        tg = FakeTelegramBot(pinned=_pinned(bot._persona_tag("tom")))
        app = SimpleNamespace(bot=tg)
        self.assertEqual(await bot._get_chat_persona(app, 7), "tom")
        tg.pinned = _pinned(bot._persona_tag("dak"))
        self.assertEqual(await bot._get_chat_persona(app, 7), "tom")
        self.assertEqual(bot.SETTINGS_MESSAGE_ID[7], 55)

    async def test_get_chat_failure_falls_back_to_default(self):
        app = SimpleNamespace(bot=FakeTelegramBot(fail_get_chat=True))
        self.assertEqual(await bot._get_chat_persona(app, 7), bot.DEFAULT_PERSONA)

    async def test_save_without_existing_pin_sends_and_pins_silently(self):
        tg = FakeTelegramBot()
        await bot._save_chat_persona(SimpleNamespace(bot=tg), 7, "leng")
        self.assertEqual(bot.CHAT_PERSONA[7], "leng")
        self.assertEqual(len(tg.sent), 1)
        self.assertIn("#persona=leng", tg.sent[0][0])
        self.assertEqual(tg.pins[0][1].get("disable_notification"), True)
        self.assertEqual(bot.SETTINGS_MESSAGE_ID[7], tg.pins[0][0])

    async def test_save_with_own_pin_edits_it_instead(self):
        tg = FakeTelegramBot()
        bot.SETTINGS_MESSAGE_ID[7] = 55
        await bot._save_chat_persona(SimpleNamespace(bot=tg), 7, "dak")
        self.assertEqual(tg.edits, [(55, bot._persona_tag("dak"))])
        self.assertEqual(tg.sent, [])

    async def test_save_when_edit_fails_sends_and_pins_a_new_one(self):
        tg = FakeTelegramBot(fail_edit=True)
        bot.SETTINGS_MESSAGE_ID[7] = 55
        await bot._save_chat_persona(SimpleNamespace(bot=tg), 7, "tom")
        self.assertEqual(len(tg.pins), 1)
        self.assertNotEqual(bot.SETTINGS_MESSAGE_ID[7], 55)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset()

    async def test_settings_command_shows_keyboard(self):
        replies = []

        async def reply_text(text, reply_markup=None):
            replies.append((text, reply_markup))

        tg = FakeTelegramBot()
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=7), message=SimpleNamespace(reply_text=reply_text))
        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset()):
            await bot.settings(update, SimpleNamespace(application=SimpleNamespace(bot=tg)))
        self.assertEqual(len(replies), 1)
        self.assertIsNotNone(replies[0][1])

    async def _press(self, data, chat_id=7, allowed=frozenset()):
        answered, edited = [], []

        async def answer(text=None):
            answered.append(text)

        async def edit_message_reply_markup(reply_markup=None):
            edited.append(reply_markup)

        query = SimpleNamespace(data=data, answer=answer, edit_message_reply_markup=edit_message_reply_markup)
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), callback_query=query)
        tg = FakeTelegramBot()
        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", allowed):
            await bot.on_persona_button(update, SimpleNamespace(application=SimpleNamespace(bot=tg)))
        return answered, edited, tg

    async def test_button_sets_persists_and_rerenders(self):
        answered, edited, tg = await self._press("persona:dak")
        self.assertEqual(bot.CHAT_PERSONA[7], "dak")
        self.assertEqual(len(tg.pins), 1)
        self.assertEqual(len(answered), 1)
        self.assertIn("✓", [b.text for row in edited[0].inline_keyboard for b in row if "พี่แด๊ก" in b.text][0])

    async def test_button_from_stranger_is_ignored(self):
        answered, edited, tg = await self._press("persona:dak", chat_id=5, allowed=frozenset({7}))
        self.assertNotIn(5, bot.CHAT_PERSONA)
        self.assertEqual(tg.pins, [])
        self.assertEqual(edited, [])

    async def test_unknown_button_is_ignored(self):
        answered, edited, tg = await self._press("persona:bogus")
        self.assertNotIn(7, bot.CHAT_PERSONA)
        self.assertEqual(tg.pins, [])


class StatusReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset()

    async def test_status_names_this_chats_reader(self):
        replies = []

        async def reply_text(text, **_kw):
            replies.append(text)

        bot.CHAT_PERSONA[7] = "leng"
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=7), message=SimpleNamespace(reply_text=reply_text))
        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset()):
            await bot.status(update, SimpleNamespace(application=SimpleNamespace(bot=FakeTelegramBot())))
        self.assertIn("ป้าเล้ง", replies[0])


class RequestBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_body_carries_voice_style_and_transcript_header(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_gemini_ok(_wav()))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await bot._post_gemini(client, "สวัสดี", persona="tom")
        part = seen[0]["contents"][0]["parts"][0]
        self.assertEqual(part["text"], "## Transcript:\nสวัสดี")
        self.assertEqual(part["speechMetadata"]["style"], bot.PERSONA_BY_KEY["tom"].style)
        self.assertEqual(seen[0]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Achird")

    async def test_halving_keeps_the_persona(self):
        personas = []

        async def fake_post(_client, text, persona=None):
            personas.append(persona)
            if len(text) > 3000:
                raise bot.GeminiQuotaError("tokens", 30.0, "tpm")
            return _wav(0.1)

        pacer = bot.GeminiPacer(min_interval=0, clock=lambda: 0.0, sleep=mock.AsyncMock())
        with mock.patch.object(bot, "_post_gemini", fake_post):
            await bot._synthesize_gemini_part(("คำไทย " * 1000).strip(), pacer, persona="leng")
        self.assertGreaterEqual(len(personas), 3)
        self.assertEqual(set(personas), {"leng"})


class JobSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset()

    async def _flush(self, saved):
        bot.CHAT_PERSONA[7] = saved
        bot.PENDING_BUFFERS[7] = bot.PendingChatBuffer(texts=["สวัสดีครับ"], status_message_id=5)
        queue = asyncio.Queue()

        class FakeBot(FakeTelegramBot):
            async def edit_message_text(self, **_kw):
                pass

        with mock.patch.object(bot, "GEMINI_API_KEY", "k"), mock.patch.object(bot, "COLLECT_WINDOW_SECONDS", 0), \
                mock.patch.object(bot, "JOB_QUEUE", queue), mock.patch.object(bot, "_ensure_worker", lambda app: None), \
                mock.patch.object(bot.random, "choice", lambda keys: "dak"):
            await bot._flush_chat_after_delay(7, SimpleNamespace(bot=FakeBot()))
        return queue.get_nowait()

    async def test_job_snapshots_the_chat_persona(self):
        self.assertEqual((await self._flush("tom")).persona, "tom")

    async def test_random_is_resolved_once_at_enqueue(self):
        self.assertEqual((await self._flush("random")).persona, "dak")

    async def test_process_job_passes_the_job_persona(self):
        got = []

        async def fake_gem(part, pacer, persona=None):
            got.append(persona)
            return b"\xff\xfb\x90", 1

        class App:
            bot = FakeTelegramBot()

        async def send_voice(**kw):
            pass
        App.bot.send_voice = send_voice
        job = bot.TTSJob(chat_id=1, status_message_id=2, text="", parts=["a", "b"], queued_at=None,
                         engine="gemini", persona="leng")
        with mock.patch.object(bot, "_synthesize_gemini_part", fake_gem), \
                mock.patch.object(bot, "USAGE_METER", bot.UsageMeter(1_000_000, "Asia/Bangkok", client=None)):
            await bot._process_job(App(), job)
        self.assertEqual(got, ["leng", "leng"])


if __name__ == "__main__":
    unittest.main()
