"""ด่าน relay ต้องหยุดของที่ควรถูกปฏิเสธได้จริง — และต้องแดงด้วย *เหตุผลที่ตั้งใจ*

## ทำไมทุกเทสต์ assert `reason` ไม่ใช่แค่ `ok is False`

2026-07-30 เช้า หนูซ้อมด่าน ffmpeg ด้วย `PATH=/bin` ซึ่งบนเครื่องนี้เป็น symlink ไป
`/usr/bin` ⇒ ffmpeg ยังหาเจอ **ฉากซ้อมไม่เป็นศัตรูจริง แต่ log ขึ้นเขียวสวย**
ถ้าเทสต์ "ลายเซ็นผิด" แดงเพราะ `missing_header` เท่ากับด่านลายเซ็น**ยังไม่เคยถูกทดสอบ**
⇒ `assertEqual(d.reason, "bad_signature")` คือสิ่งที่ทำให้ความต่างนี้มองเห็น

## mutation-replay (การ์ด Brain ของฝูง 2 ใบ + positive control ของ Gen1)

เริ่มจากคำขอที่ **ผ่านจริง** (positive control) แล้ว mutate ทีละมิติ
⇒ ถ้า positive control ไม่เขียว เทสต์ปฏิเสธทั้งหมดไม่มีความหมาย (มันจะแดงอยู่ดี)
"""
import unittest

import relay_auth as ra

SECRET = "s3cr3t-for-tests-only-not-a-real-key"
CHAT = 8221290993
ALLOW = frozenset({CHAT})
BODY = b"OggS\x00fake-opus-bytes-for-tests"
NOW = 1_754_000_000.0          # เวลาตายตัว — ไม่ใช้ time.time() ในเทสต์ ไม่งั้น skew flaky
NONCE = "abcdef0123456789beef"  # >= MIN_NONCE_LEN


def signed(*, secret=SECRET, chat_id=CHAT, body=BODY, nonce=NONCE, ts=NOW):
    return ra.sign_request(secret, chat_id, body, nonce=nonce, ts=ts)


def check(headers, body=BODY, *, secret=SECRET, allow=ALLOW, cache=None, now=NOW):
    return ra.verify(headers, body, secret=secret, allowed_chat_ids=allow,
                     nonce_cache=cache if cache is not None else ra.NonceCache(), now=now)


class PositiveControl(unittest.TestCase):
    """ถ้าคลาสนี้แดง ห้ามเชื่อผลของคลาสอื่นเลย"""

    def test_valid_request_passes(self):
        d = check(signed())
        self.assertTrue(d.ok, msg=f"positive control ต้องผ่าน แต่ได้ {d.reason}: {d.detail}")
        self.assertEqual(d.reason, "ok")
        self.assertEqual(d.status, 200)
        self.assertEqual(d.chat_id, CHAT)

    def test_decision_is_truthy_so_check_and_do_reads_naturally(self):
        # ด่านต้องต่อกับการกระทำได้: `if verify(...): send()` ไม่ใช่ `verify(...); send()`
        self.assertTrue(bool(check(signed())))
        self.assertFalse(bool(check(signed(secret="wrong-secret"))))

    def test_header_names_are_case_insensitive(self):
        h = {k.upper(): v for k, v in signed().items()}
        self.assertTrue(check(h).ok)

    def test_sender_and_verifier_agree(self):
        # เหตุผลที่ sign_request อยู่ไฟล์เดียวกับ verify — ถ้าแยก วันหนึ่งจะประกอบไม่ตรงกัน
        h = signed()
        self.assertEqual(
            h[ra.HDR_SIG],
            ra.expected_signature(SECRET, h[ra.HDR_TS], h[ra.HDR_NONCE], h[ra.HDR_CHAT], BODY),
        )


class FailClosedWithoutSecret(unittest.TestCase):
    def test_no_secret_closes_endpoint_not_the_check(self):
        # คำขอนี้ "ถูกต้องทุกอย่าง" — ต้องยังถูกปฏิเสธ เพราะ relay ไม่ได้ตั้ง secret
        for missing in (None, "", "   "):
            with self.subTest(secret=missing):
                d = check(signed(), secret=missing)
                self.assertFalse(d.ok)
                self.assertEqual(d.reason, "no_secret")
                self.assertEqual(d.status, 503)

    def test_whitespace_secret_is_not_a_secret(self):
        """เทสต์นี้จับบั๊กจริงได้ก่อน commit ครั้งแรก

        `if not secret:` เพียวๆ ปล่อย `"   "` ผ่าน (truthy) ⇒ endpoint เปิดด้วยกุญแจขยะ
        ตระกูลเดียวกับ `presets.get("")` ที่ `name or DEFAULT` กลืนค่าว่าง
        """
        for blank in ("   ", "\t", "\n  \n"):
            with self.subTest(secret=repr(blank)):
                self.assertEqual(check(signed(), secret=blank).reason, "no_secret")

    def test_short_secret_closes_the_endpoint(self):
        short = "a" * (ra.MIN_SECRET_LEN - 1)
        d = check(signed(secret=short), secret=short)
        self.assertEqual(d.reason, "weak_secret")
        self.assertEqual(d.status, 503)

    def test_secret_at_minimum_length_works(self):
        ok_secret = "k" * ra.MIN_SECRET_LEN
        self.assertTrue(check(signed(secret=ok_secret), secret=ok_secret).ok)

    def test_surrounding_whitespace_in_env_var_is_tolerated(self):
        # Render/dotenv ทำให้ค่ามี \n ท้ายได้ง่ายๆ — ต้องไม่ทำให้ relay ตายเงียบ
        padded = f"  {SECRET}\n"
        self.assertTrue(check(signed(secret=SECRET), secret=padded).ok)

    def test_no_secret_wins_over_every_other_error(self):
        # ลำดับสำคัญ: ไม่มี secret ต้องตอบก่อน แม้คำขอจะพิการด้วย
        d = check({}, body=b"", secret=None)
        self.assertEqual(d.reason, "no_secret")


class RequestShapeRejections(unittest.TestCase):
    def test_each_missing_header_is_caught(self):
        for name in (ra.HDR_TS, ra.HDR_NONCE, ra.HDR_CHAT, ra.HDR_SIG):
            with self.subTest(header=name):
                h = signed()
                del h[name]
                d = check(h)
                self.assertEqual(d.reason, "missing_header")
                self.assertEqual(d.status, 400)
                self.assertIn(name, d.detail)

    def test_blank_header_counts_as_missing(self):
        h = signed()
        h[ra.HDR_SIG] = "   "
        self.assertEqual(check(h).reason, "missing_header")

    def test_short_nonce_rejected(self):
        short = "a" * (ra.MIN_NONCE_LEN - 1)
        self.assertEqual(check(signed(nonce=short), ).reason, "nonce_too_short")

    def test_nonce_at_minimum_length_is_accepted(self):
        # ขอบเขตอีกด้าน — ถ้าไม่เทสต์ ด่านอาจแน่นเกินไปแล้วไม่มีใครรู้
        ok_nonce = "b" * ra.MIN_NONCE_LEN
        self.assertTrue(check(signed(nonce=ok_nonce)).ok)

    def test_empty_body_rejected(self):
        self.assertEqual(check(signed(body=b""), body=b"").reason, "empty_body")

    def test_oversize_body_rejected_before_hashing_50mb(self):
        big = b"x" * (ra.MAX_BODY_BYTES + 1)
        d = check(signed(body=big), body=big)
        self.assertEqual(d.reason, "body_too_large")
        self.assertEqual(d.status, 413)

    def test_malformed_timestamp(self):
        h = signed()
        h[ra.HDR_TS] = "เมื่อกี้"
        self.assertEqual(check(h).reason, "malformed_timestamp")

    def test_malformed_chat_id(self):
        h = signed()
        h[ra.HDR_CHAT] = "8221290993.5"
        self.assertEqual(check(h).reason, "malformed_chat_id")


class TimestampWindow(unittest.TestCase):
    def test_too_old_rejected(self):
        d = check(signed(ts=NOW - ra.MAX_SKEW_SECONDS - 1))
        self.assertEqual(d.reason, "stale_timestamp")
        self.assertEqual(d.status, 401)

    def test_too_far_in_future_rejected(self):
        # ไม่ใช่แค่ของเก่า — ts อนาคตไกลก็ขยายหน้าต่าง replay ได้
        self.assertEqual(check(signed(ts=NOW + ra.MAX_SKEW_SECONDS + 1)).reason, "stale_timestamp")

    def test_just_inside_window_passes(self):
        self.assertTrue(check(signed(ts=NOW - ra.MAX_SKEW_SECONDS + 1)).ok)


class MutationReplay(unittest.TestCase):
    """mutate ทีละมิติจากคำขอที่ผ่าน — ทุกครั้งต้องได้ `bad_signature` เป๊ะ

    ถ้ามิติไหน mutate แล้วยัง **ผ่าน** = ลายเซ็นไม่ครอบมิตินั้น
    ถ้ามิติไหน mutate แล้วแดงด้วย reason อื่น = ด่านลายเซ็นยังไม่ถูกทดสอบในมิตินั้น
    """

    def test_wrong_secret(self):
        self.assertEqual(check(signed(secret="not-the-secret")).reason, "bad_signature")

    def test_body_tampered_by_one_byte(self):
        h = signed()
        tampered = BODY[:-1] + bytes([BODY[-1] ^ 0x01])
        self.assertEqual(len(tampered), len(BODY))          # เปลี่ยนเนื้อ ไม่เปลี่ยนขนาด
        self.assertEqual(check(h, body=tampered).reason, "bad_signature")

    def test_chat_id_swapped_to_another_allowed_chat(self):
        # สำคัญ: chat ปลายทางใหม่ **อยู่ใน allowlist** ⇒ ถ้าลายเซ็นไม่ครอบ chat_id
        # คนที่ดักไฟล์ได้จะเปลี่ยนปลายทางไปแชทอื่นที่อนุญาตไว้ได้
        other = 111222333
        h = signed()
        h[ra.HDR_CHAT] = str(other)
        d = check(h, allow=frozenset({CHAT, other}))
        self.assertEqual(d.reason, "bad_signature")

    def test_nonce_swapped(self):
        h = signed()
        h[ra.HDR_NONCE] = "ffffffff0000000011112222"
        self.assertEqual(check(h).reason, "bad_signature")

    def test_timestamp_shifted_inside_window(self):
        # ยังอยู่ในหน้าต่าง skew ⇒ ถ้าแดง ต้องแดงเพราะลายเซ็น ไม่ใช่เพราะ stale
        h = signed()
        h[ra.HDR_TS] = repr(NOW + 1.0)
        self.assertEqual(check(h).reason, "bad_signature")

    def test_truncated_signature(self):
        h = signed()
        h[ra.HDR_SIG] = h[ra.HDR_SIG][:-2]
        self.assertEqual(check(h).reason, "bad_signature")

    def test_signature_of_right_length_but_wrong_value(self):
        h = signed()
        h[ra.HDR_SIG] = "0" * len(h[ra.HDR_SIG])
        self.assertEqual(check(h).reason, "bad_signature")


class NonceReplay(unittest.TestCase):
    def test_same_request_twice_is_rejected_the_second_time(self):
        cache = ra.NonceCache()
        h = signed()
        self.assertTrue(check(h, cache=cache).ok)
        d = check(h, cache=cache)
        self.assertEqual(d.reason, "replayed_nonce")
        self.assertEqual(d.status, 409)

    def test_rejected_request_does_not_burn_the_nonce(self):
        """ทำไม nonce ต้องตรวจ *หลัง* ลายเซ็น

        ถ้าตรวจก่อน คนที่ไม่มี secret ยิง nonce ของผู้ส่งจริงล่วงหน้าได้
        ⇒ เผา nonce ทิ้ง ⇒ ของจริงถูกปฏิเสธ = DoS ที่ไม่ต้องมีกุญแจ
        """
        cache = ra.NonceCache()
        self.assertEqual(check(signed(secret="attacker"), cache=cache).reason, "bad_signature")
        self.assertEqual(len(cache), 0, "คำขอที่ลายเซ็นผิดต้องไม่ถูกจดลง cache")
        self.assertTrue(check(signed(), cache=cache).ok, "ของจริงต้องยังส่งได้")

    def test_chat_not_allowed_does_not_burn_the_nonce(self):
        # ผ่านลายเซ็นแล้วแต่ตกที่ allowlist — ก็ยังไม่ควรจด (คำขอนั้นไม่เกิดผลอะไร)
        cache = ra.NonceCache()
        self.assertEqual(check(signed(), allow=frozenset(), cache=cache).reason, "chat_not_allowed")
        self.assertEqual(len(cache), 0)

    def test_different_nonce_same_second_is_fine(self):
        cache = ra.NonceCache()
        self.assertTrue(check(signed(), cache=cache).ok)
        self.assertTrue(check(signed(nonce="0011223344556677aabb"), cache=cache).ok)

    def test_full_cache_rejects_instead_of_flushing(self):
        # ล้างทิ้งตอนเต็ม = เปิดช่อง replay ⇒ ต้อง fail closed
        cache = ra.NonceCache(max_entries=1)
        self.assertTrue(check(signed(), cache=cache).ok)
        d = check(signed(nonce="cccccccccccccccccccc"), cache=cache)
        self.assertEqual(d.reason, "nonce_cache_full")
        self.assertEqual(d.status, 503)

    def test_purge_drops_nonces_older_than_the_window(self):
        cache = ra.NonceCache()
        cache.remember("old-nonce-aaaaaaaaaa", NOW)
        cache.purge(NOW + ra.MAX_SKEW_SECONDS * 2 + 1)
        self.assertFalse(cache.seen("old-nonce-aaaaaaaaaa"))
        self.assertEqual(len(cache), 0)


class Allowlist(unittest.TestCase):
    def test_empty_allowlist_denies_everyone(self):
        d = check(signed(), allow=frozenset())
        self.assertEqual(d.reason, "chat_not_allowed")
        self.assertEqual(d.status, 403)

    def test_unlisted_chat_denied(self):
        stranger = 999888777
        d = check(signed(chat_id=stranger), allow=ALLOW)
        self.assertEqual(d.reason, "chat_not_allowed")
        self.assertEqual(d.chat_id, stranger)

    def test_relay_default_is_deliberately_stricter_than_bot(self):
        """เอกสารความต่างที่ตั้งใจ — และคุมไว้ด้วยเทสต์ทั้งสองฝั่ง

        `bot.py` allowlist ว่าง = ให้ทุกคน (คนต้องรู้ชื่อ bot ก่อนจะคุยได้)
        `relay` allowlist ว่าง = **ปฏิเสธทุกคน** (รับจากอินเทอร์เน็ตทั้งใบ)
        ถ้าวันหนึ่งใครทำให้เหมือนกัน เทสต์นี้จะแดงและบังคับให้เจตนาถูกพูดออกมา
        """
        import os
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
        os.environ.setdefault("GOOGLE_API_KEY", "test-key")
        import bot
        from unittest import mock

        with mock.patch.object(bot, "ALLOWED_CHAT_IDS", frozenset()):
            self.assertTrue(bot._is_allowed_chat(CHAT), "bot: ว่าง = ให้ทุกคน")
        self.assertFalse(check(signed(), allow=frozenset()).ok, "relay: ว่าง = ปฏิเสธทุกคน")


class TimingSafeCompareIsSourceLevelOnly(unittest.TestCase):
    """🟡 ด่านนี้อ่อนกว่าใบอื่นในไฟล์นี้ และต้องพูดออกมา

    mutation-replay 2026-07-30 พบว่า mutant `compare_digest -> ==` **SURVIVED**
    เทสต์เชิงพฤติกรรมทั้ง 39 ใบ — เพราะผลลัพธ์ (ปฏิเสธ/ผ่าน) เหมือนกันเป๊ะ
    ต่างกันแค่ *เวลา* ที่ใช้เทียบ ⇒ วัดด้วย assert เรื่องพฤติกรรมไม่ได้เลย

    ⇒ ใบนี้จึงเป็น **ด่านระดับ source ไม่ใช่ระดับพฤติกรรม** · มันฆ่า mutant นั้นได้
    แต่กันคนที่เขียน timing leak ด้วยวิธีอื่นไม่ได้ · จดไว้ว่านี่คือขอบเขตของมัน
    ไม่ใช่ปล่อยให้อ่านเหมือนว่าครอบเท่าใบอื่น
    """

    def test_signature_comparison_uses_compare_digest_not_equality(self):
        import inspect
        src = inspect.getsource(ra.verify)
        self.assertIn("hmac.compare_digest", src,
                      "ต้องเทียบลายเซ็นแบบเวลาคงที่")
        for leak in ("expect == sig", "expect != sig", "sig == expect", "sig != expect"):
            self.assertNotIn(leak, src, f"พบการเทียบตรงๆ ({leak}) — timing leak")


class CheckOrderIsPartOfSecurity(unittest.TestCase):
    def test_bad_signature_reported_before_allowlist(self):
        # ไม่รั่วว่า chat ไหนอยู่ใน allowlist ให้คนที่ไม่มีกุญแจ
        d = check(signed(secret="attacker", chat_id=424242), allow=ALLOW)
        self.assertEqual(d.reason, "bad_signature")

    def test_stale_timestamp_reported_before_signature(self):
        # ts เป็นของผู้เรียกเอง ตรวจก่อนได้ และมันจำกัดหน้าต่างที่ nonce ต้องจำ
        h = signed(secret="attacker", ts=NOW - 10_000)
        self.assertEqual(check(h).reason, "stale_timestamp")


if __name__ == "__main__":
    unittest.main()
