import asyncio
import base64
import re
import io
import json
import os
import unittest
import wave
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import httpx

import bot


def _wav(seconds: float = 0.5, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _gemini_ok(wav: bytes) -> dict:
    return {"candidates": [{"content": {"parts": [{"inlineData": {
        "mimeType": "audio/wav", "data": base64.b64encode(wav).decode()}}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10}}


def _gemini_429(quota_id: str, delay: str = "30s") -> dict:
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
    ]}}


class GeminiSplitTests(unittest.TestCase):
    def test_gemini_parts_are_at_most_part_size_and_lossless(self):
        text = "ย่อหน้า " + ("ภาษาไทยยาวมาก " * 2200)   # ~30k chars

        parts = bot._split_gemini_parts(text)

        self.assertEqual(bot.GEMINI_PART_SIZE, 14000)
        self.assertGreaterEqual(len(parts), 3)
        self.assertLessEqual(max(len(p) for p in parts), bot.GEMINI_PART_SIZE + 1)
        self.assertEqual(" ".join(p.rstrip(".") for p in parts).split(), text.split())

    def test_short_text_is_one_gemini_part(self):
        self.assertEqual(len(bot._split_gemini_parts("สวัสดีครับ " * 1000)), 1)


class WavToMp3Tests(unittest.TestCase):
    def test_wav_becomes_mp3_frames(self):
        mp3 = bot._wav_to_mp3(_wav(1.0))

        self.assertGreater(len(mp3), 100)
        self.assertEqual(mp3[0], 0xFF)                 # MPEG frame sync
        self.assertEqual(mp3[1] & 0xE0, 0xE0)
        self.assertNotEqual(mp3[:4], b"RIFF")


class PostGeminiTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_success_returns_wav_and_sends_voice_without_count_tokens(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=_gemini_ok(_wav()))

        async with await self._client(handler) as client:
            wav = await bot._post_gemini(client, "สวัสดี")

        self.assertEqual(wav, _wav())                # passed through untouched — no second RIFF header
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].url.path.endswith(f"models/{bot.GEMINI_MODEL}:generateContent"))
        self.assertNotIn("countTokens", str(seen[0].url))
        body = json.loads(seen[0].content)
        voice = body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
        self.assertEqual(voice, bot.GEMINI_VOICE)
        self.assertEqual(body["generationConfig"]["responseModalities"], ["AUDIO"])

    async def test_429_per_minute_raises_tpm_with_retry_delay(self):
        def handler(request):
            return httpx.Response(429, json=_gemini_429("GenerateContentInputTokensPerModelPerMinute-FreeTier", "31s"))

        async with await self._client(handler) as client:
            with self.assertRaises(bot.GeminiQuotaError) as ctx:
                await bot._post_gemini(client, "x")
        self.assertEqual(ctx.exception.kind, "tokens")
        self.assertEqual(ctx.exception.retry_delay, 31.0)

    async def test_429_per_day_raises_day(self):
        def handler(request):
            return httpx.Response(429, json=_gemini_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier"))

        async with await self._client(handler) as client:
            with self.assertRaises(bot.GeminiQuotaError) as ctx:
                await bot._post_gemini(client, "x")
        self.assertEqual(ctx.exception.kind, "day")

    async def test_other_http_error_raises_runtime_error(self):
        def handler(request):
            return httpx.Response(400, json={"error": {"message": "bad"}})

        async with await self._client(handler) as client:
            with self.assertRaises(RuntimeError):
                await bot._post_gemini(client, "x")

    def test_gemini_read_timeout_outlasts_a_long_request(self):
        self.assertGreaterEqual(bot.GEMINI_READ_TIMEOUT, 120)   # measured 23–51 s per 4k–14k chunk


class PostGeminiRobustnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_requests_per_minute_is_kind_minute(self):
        def handler(request):
            return httpx.Response(429, json=_gemini_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "12s"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(bot.GeminiQuotaError) as ctx:
                await bot._post_gemini(client, "x")
        self.assertEqual((ctx.exception.kind, ctx.exception.retry_delay), ("minute", 12.0))

    async def test_200_without_audio_raises_a_readable_error(self):
        def handler(request):
            return httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RuntimeError) as ctx:
                await bot._post_gemini(client, "x")
        self.assertIn("no audio", str(ctx.exception))
        self.assertIn("SAFETY", str(ctx.exception))

    async def test_non_stop_finish_reason_is_logged(self):
        body = _gemini_ok(_wav())
        body["candidates"][0]["finishReason"] = "MAX_TOKENS"

        def handler(request):
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertLogs(bot.LOGGER, level="WARNING") as logs:
                await bot._post_gemini(client, "x")
        self.assertTrue(any("MAX_TOKENS" in line for line in logs.output))


class SliceEncodeTests(unittest.TestCase):
    def test_wav_is_encoded_in_bounded_slices(self):
        fed = []

        class FakeEncoder:
            def __getattr__(self, name):
                return lambda *a: None

            def encode(self, pcm):
                fed.append(len(pcm))
                return b"\xff\xfb"

            def flush(self):
                return b""

        wav = _wav(95.0)   # 95 s at 24 kHz s16 mono
        with mock.patch.object(bot.lameenc, "Encoder", FakeEncoder):
            bot._wav_to_mp3(wav)

        self.assertGreater(len(fed), 1)
        self.assertLessEqual(max(fed), 30 * 24000 * 2)
        self.assertEqual(sum(fed), 95 * 24000 * 2)


class SegmentAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_half_contributes_its_audio_in_order(self):
        async def fake_post(_client, text):
            if len(text) > 3000:
                raise bot.GeminiQuotaError("tokens", 30.0, "tpm")
            return f"WAV{len(text)}".encode()

        pacer = bot.GeminiPacer(min_interval=0, clock=lambda: 0.0, sleep=mock.AsyncMock())
        text = ("คำไทย " * 1000).strip()
        with mock.patch.object(bot, "_post_gemini", fake_post), \
                mock.patch.object(bot, "_wav_to_mp3", lambda w: b"[" + w + b"]"):
            mp3, requests = await bot._synthesize_gemini_part(text, pacer)

        pieces = mp3.decode().strip("[]").split("][")
        self.assertGreaterEqual(len(pieces), 2)
        self.assertEqual(sum(int(p[3:]) for p in pieces), len(text) + len(pieces) - 1 - text.count("  "))

    async def test_short_part_is_not_halved_on_token_quota(self):
        calls = []

        async def fake_post(_client, text):
            calls.append(text)
            raise bot.GeminiQuotaError("tokens", 30.0, "tpm")

        pacer = bot.GeminiPacer(min_interval=0, clock=lambda: 0.0, sleep=mock.AsyncMock())
        with mock.patch.object(bot, "_post_gemini", fake_post):
            with self.assertRaises(bot.GeminiQuotaError):
                await bot._synthesize_gemini_part("สั้น " * 100, pacer)   # < 2 × GEMINI_MIN_SPLIT
        self.assertEqual(len(calls), 1)

    async def test_halves_are_paced(self):
        now, slept = [0.0], []

        async def fake_sleep(s):
            slept.append(s)
            now[0] += s

        async def fake_post(_client, text):
            now[0] += 1
            if len(text) > 3000:
                raise bot.GeminiQuotaError("tokens", 30.0, "tpm")
            return _wav(0.1)

        pacer = bot.GeminiPacer(min_interval=60, clock=lambda: now[0], sleep=fake_sleep)
        with mock.patch.object(bot, "_post_gemini", fake_post):
            _mp3, requests = await bot._synthesize_gemini_part(("คำไทย " * 1000).strip(), pacer)
        self.assertEqual(len(slept), requests - 1)
        self.assertTrue(all(s >= 58 for s in slept))

    async def test_minute_429_waits_retry_delay_and_retries_same_size(self):
        calls, slept = [], []

        async def fake_post(_client, text):
            calls.append(len(text))
            if len(calls) == 1:
                raise bot.GeminiQuotaError("minute", 12.0, "rpm")
            return _wav(0.2)

        async def fake_sleep(s):
            slept.append(s)

        pacer = bot.GeminiPacer(min_interval=0, clock=lambda: 0.0, sleep=fake_sleep)
        with mock.patch.object(bot, "_post_gemini", fake_post):
            mp3, requests = await bot._synthesize_gemini_part("สวัสดี " * 800, pacer)

        self.assertEqual(calls[0], calls[1])
        self.assertIn(12.0, slept)
        self.assertEqual(requests, 2)

    async def test_encoding_runs_off_the_event_loop(self):
        seen = []
        real_to_thread = bot.asyncio.to_thread

        async def spy(func, *args):
            seen.append(func.__name__)
            return await real_to_thread(func, *args)

        async def fake_post(_client, text):
            return _wav(0.2)

        pacer = bot.GeminiPacer(min_interval=0, clock=lambda: 0.0, sleep=mock.AsyncMock())
        with mock.patch.object(bot, "_post_gemini", fake_post), mock.patch.object(bot.asyncio, "to_thread", spy):
            await bot._synthesize_gemini_part("x", pacer)
        self.assertIn("_wav_to_mp3", seen)


class PacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_request_waits_out_the_interval(self):
        now = [1000.0]
        slept = []

        async def fake_sleep(s):
            slept.append(s)
            now[0] += s

        pacer = bot.GeminiPacer(min_interval=60, clock=lambda: now[0], sleep=fake_sleep)
        await pacer.wait()
        now[0] += 10
        await pacer.wait()

        self.assertEqual(slept, [50])

    async def test_first_request_does_not_wait(self):
        slept = []

        async def fake_sleep(s):
            slept.append(s)

        await bot.GeminiPacer(min_interval=60, clock=lambda: 0.0, sleep=fake_sleep).wait()
        self.assertEqual(slept, [])


class SynthesizeGeminiPartTests(unittest.IsolatedAsyncioTestCase):
    async def test_minute_quota_halves_the_part_and_joins_audio(self):
        calls = []

        async def fake_post(_client, text):
            calls.append(len(text))
            if len(text) > 3000:
                raise bot.GeminiQuotaError("tokens", 30.0, "tpm")
            return _wav(0.2)

        pacer = bot.GeminiPacer(min_interval=0, clock=lambda: 0.0, sleep=mock.AsyncMock())
        text = ("คำไทย " * 1000).strip()   # ~6000 chars
        with mock.patch.object(bot, "_post_gemini", fake_post):
            mp3, requests = await bot._synthesize_gemini_part(text, pacer)

        self.assertEqual(calls[0], len(text))
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all(c < len(text) for c in calls[1:]))
        self.assertEqual(requests, len(calls))
        self.assertEqual(mp3[0], 0xFF)

    async def test_day_quota_propagates(self):
        async def fake_post(_client, text):
            raise bot.GeminiQuotaError("day", None, "rpd")

        pacer = bot.GeminiPacer(min_interval=0, clock=lambda: 0.0, sleep=mock.AsyncMock())
        with mock.patch.object(bot, "_post_gemini", fake_post):
            with self.assertRaises(bot.GeminiQuotaError) as ctx:
                await bot._synthesize_gemini_part("x", pacer)
        self.assertEqual(ctx.exception.kind, "day")


class ProcessJobGeminiTests(unittest.IsolatedAsyncioTestCase):
    def _fake_app(self):
        sent, messages = [], []

        class FakeBot:
            async def edit_message_text(self, chat_id, message_id, text):
                messages.append(text)

            async def send_message(self, chat_id, text):
                messages.append(text)

            async def send_voice(self, chat_id, voice, caption, **_kw):
                sent.append((voice.name, caption, voice.getvalue()[:3]))

        class FakeApp:
            bot = FakeBot()

        return FakeApp(), sent, messages

    async def test_gemini_job_sends_one_mp3_per_part(self):
        app, sent, _ = self._fake_app()

        async def fake_gem(part, pacer):
            return b"\xff\xfb\x90" + b"\x00" * 10, 1

        job = bot.TTSJob(chat_id=1, status_message_id=2, text="a b", parts=["ส่วนหนึ่ง", "ส่วนสอง"],
                         queued_at=None, engine="gemini")
        with mock.patch.object(bot, "_synthesize_gemini_part", fake_gem), \
                mock.patch.object(bot, "USAGE_METER", bot.UsageMeter(1_000_000, "Asia/Bangkok", client=None)):
            await bot._process_job(app, job)

        self.assertEqual(len(sent), 2)
        self.assertTrue(all(name.endswith(".mp3") for name, _, _ in sent))

    async def test_daily_quota_falls_back_to_chirp_for_the_rest_and_says_so(self):
        app, sent, messages = self._fake_app()
        gem_calls, chirp_calls = [], []

        async def fake_gem(part, pacer):
            gem_calls.append(part)
            if len(gem_calls) == 2:
                raise bot.GeminiQuotaError("day", None, "rpd")
            return b"\xff\xfbG", 1

        async def fake_chirp(part, progress=None):
            chirp_calls.append(part)
            return b"\xff\xfbC", len(part), 1, bot.USAGE_METER.preview()

        job = bot.TTSJob(chat_id=1, status_message_id=2, text="", parts=["หนึ่ง", "สอง", "สาม"],
                         queued_at=None, engine="gemini")
        with mock.patch.object(bot, "_synthesize_gemini_part", fake_gem), \
                mock.patch.object(bot, "_synthesize_part_with_timeout", fake_chirp), \
                mock.patch.object(bot, "USAGE_METER", bot.UsageMeter(1_000_000, "Asia/Bangkok", client=None)):
            await bot._process_job(app, job)

        self.assertEqual(gem_calls, ["หนึ่ง", "สอง"])
        self.assertEqual(re.sub(r"[\s.]", "", "".join(chirp_calls)), "สองสาม")   # part 2 and 3, no loss
        self.assertEqual([s[2] for s in sent][0], b"\xff\xfbG")
        self.assertTrue(all(s[2] == b"\xff\xfbC" for s in sent[1:]))
        self.assertTrue(any("Chirp" in m for m in messages), messages)


class GeminiFailureFallbackTests(unittest.IsolatedAsyncioTestCase):
    def _app(self):
        edits, sends, voices = [], [], []

        class FakeBot:
            async def edit_message_text(self, chat_id, message_id, text):
                edits.append(text)

            async def send_message(self, chat_id, text):
                sends.append(text)

            async def send_voice(self, chat_id, voice, caption, **_kw):
                voices.append(voice.getvalue()[:3])

        class FakeApp:
            bot = FakeBot()

        return FakeApp(), edits, sends, voices

    async def _run(self, error):
        app, edits, sends, voices = self._app()

        async def fake_gem(part, pacer):
            raise error

        async def fake_chirp(part, progress=None):
            return b"\xff\xfbC", len(part), 1, bot.USAGE_METER.preview()

        job = bot.TTSJob(chat_id=1, status_message_id=2, text="", parts=["หนึ่ง", "สอง"], queued_at=None, engine="gemini")
        with mock.patch.object(bot, "_synthesize_gemini_part", fake_gem), \
                mock.patch.object(bot, "_synthesize_part_with_timeout", fake_chirp), \
                mock.patch.object(bot, "USAGE_METER", bot.UsageMeter(1_000_000, "Asia/Bangkok", client=None)):
            await bot._process_job(app, job)
        return edits, sends, voices

    async def test_server_error_falls_back_to_chirp(self):
        edits, sends, voices = await self._run(RuntimeError("Gemini TTS 503: overloaded"))
        self.assertTrue(voices and all(v == b"\xff\xfbC" for v in voices))
        self.assertTrue(any("Chirp" in m for m in sends), sends)

    async def test_timeout_falls_back_to_chirp(self):
        edits, sends, voices = await self._run(httpx.ReadTimeout("slow"))
        self.assertTrue(voices)
        self.assertTrue(any("Chirp" in m for m in sends), sends)

    async def test_day_quota_notice_is_its_own_message(self):
        edits, sends, voices = await self._run(bot.GeminiQuotaError("day", None, "rpd"))
        self.assertTrue(any("Chirp" in m for m in sends), sends)


class ChirpFallbackSizeTests(unittest.IsolatedAsyncioTestCase):
    _fake_app = ProcessJobGeminiTests._fake_app

    async def test_fallback_resplits_a_gemini_sized_part_for_chirp(self):
        app, sent, messages = self._fake_app()
        chirp_parts = []

        async def fake_gem(part, pacer):
            raise bot.GeminiQuotaError("day", None, "rpd")

        async def fake_chirp(part, progress=None):
            chirp_parts.append(part)
            return b"\xff\xfbC", len(part), 1, bot.USAGE_METER.preview()

        big = ("ข้อความยาว " * 1300).strip()   # ~14k chars — one Gemini part
        job = bot.TTSJob(chat_id=1, status_message_id=2, text=big, parts=[big], queued_at=None, engine="gemini")
        with mock.patch.object(bot, "_synthesize_gemini_part", fake_gem), \
                mock.patch.object(bot, "_synthesize_part_with_timeout", fake_chirp), \
                mock.patch.object(bot, "USAGE_METER", bot.UsageMeter(1_000_000, "Asia/Bangkok", client=None)):
            await bot._process_job(app, job)

        self.assertGreater(len(chirp_parts), 1)
        self.assertLessEqual(max(len(p) for p in chirp_parts), bot.PART_SIZE + 1)


class FlushEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_flush_queues_gemini_parts_when_key_present(self):
        text = "ข้อความยาว " * 2000   # ~22k chars → 2 gemini parts
        bot.PENDING_BUFFERS[7] = bot.PendingChatBuffer(texts=[text], status_message_id=5)
        queue = asyncio.Queue()

        class FakeBot:
            async def edit_message_text(self, **_kw):
                pass

        class FakeApp:
            bot = FakeBot()

        with mock.patch.object(bot, "GEMINI_API_KEY", "k"), mock.patch.object(bot, "COLLECT_WINDOW_SECONDS", 0), \
                mock.patch.object(bot, "MAX_CHARS", 30000), mock.patch.object(bot, "JOB_QUEUE", queue), \
                mock.patch.object(bot, "_ensure_worker", lambda app: None):
            await bot._flush_chat_after_delay(7, FakeApp())

        job = queue.get_nowait()
        self.assertEqual(job.engine, "gemini")
        self.assertEqual(job.parts, bot._split_gemini_parts(text.strip()))
        self.assertEqual(len(job.parts), 2)

    def test_runtime_status_names_the_engine(self):
        with mock.patch.object(bot, "GEMINI_API_KEY", "k"):
            self.assertIn("gemini", bot._format_runtime_status(bot.USAGE_METER.preview()))


class EngineSelectionTests(unittest.TestCase):
    def test_engine_is_gemini_only_when_key_present(self):
        with mock.patch.object(bot, "GEMINI_API_KEY", "k"):
            self.assertEqual(bot._engine(), "gemini")
        with mock.patch.object(bot, "GEMINI_API_KEY", None):
            self.assertEqual(bot._engine(), "chirp")
        with mock.patch.object(bot, "GEMINI_API_KEY", "  "):
            self.assertEqual(bot._engine(), "chirp")


if __name__ == "__main__":
    unittest.main()
