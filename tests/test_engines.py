import asyncio
import os
import unittest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

from engines import EngineChain, EngineUnavailable, LocalF5Engine, VoiceSpec

BOOMMER = VoiceSpec(agent="Boommer", engine="local",
                    ref_wav="/nonexistent/ref.wav", ref_text="ทดสอบ")
AGENT_A = VoiceSpec(agent="AgentA", engine="google", google_voice="th-TH-Chirp3-HD-Achernar")


class FakeEngine:
    def __init__(self, name, *, unavailable=False, hard_fail=False, max_concurrency=1):
        self.name = name
        self.max_concurrency = max_concurrency
        self.calls = []
        self._unavailable = unavailable
        self._hard_fail = hard_fail

    async def synth(self, chunk, voice):
        self.calls.append((chunk, voice.agent))
        if self._unavailable:
            raise EngineUnavailable(f"{self.name} ไม่ว่าง")
        if self._hard_fail:
            raise RuntimeError(f"{self.name} คำขอผิด")
        return f"audio:{self.name}".encode()


class TranslitGateTests(unittest.TestCase):
    """ด่านต้องหยุดของได้จริง — และต้องปล่อยของที่ควรผ่าน"""

    def test_gate_rejects_residual_latin(self):
        with self.assertRaises(EngineUnavailable) as ctx:
            LocalF5Engine.check_text("ผม merge PR เข้า main แล้ว")
        self.assertIn("merge", str(ctx.exception))

    def test_gate_rejects_residual_arabic_digits(self):
        with self.assertRaises(EngineUnavailable):
            LocalF5Engine.check_text("รอ 5 นาที")

    def test_gate_rejects_commit_sha(self):
        """SHA ต้องตกด่าน ไม่ใช่ถูกอ่านทีละหลักแบบผิดๆ"""
        with self.assertRaises(EngineUnavailable):
            LocalF5Engine.check_text("ตรวจที่ f31e61b ครับ")

    def test_gate_passes_pure_thai(self):
        LocalF5Engine.check_text("รายงานสถานะ การทดสอบผ่านทั้งหมดสิบสองรายการ")

    def test_gate_passes_text_after_translit(self):
        from thai_text import translit

        LocalF5Engine.check_text(translit("ผม merge PR 122 เข้า main แล้ว"))


class LocalEngineGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_ref_file_is_unavailable_not_crash(self):
        engine = LocalF5Engine()
        with self.assertRaises(EngineUnavailable) as ctx:
            await engine.synth("ทดสอบเสียงไทย", BOOMMER)
        self.assertIn("ref audio หาย", str(ctx.exception))

    async def test_google_voice_rejected_by_local_engine(self):
        engine = LocalF5Engine()
        with self.assertRaises(EngineUnavailable):
            await engine.synth("ทดสอบเสียงไทย", AGENT_A)

    async def test_gate_runs_before_touching_gpu(self):
        """ด่านต้องตีตกก่อนเสีย GPU — ref ไม่มีจริงก็ต้องไม่ถึงขั้นโหลดโมเดล"""
        engine = LocalF5Engine()
        with self.assertRaises(EngineUnavailable):
            await engine.synth("merge PR", BOOMMER)
        self.assertIsNone(engine._tts)

    def test_max_concurrency_is_one_by_design(self):
        self.assertEqual(LocalF5Engine.max_concurrency, 1)


class EngineChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_success_never_touches_fallback(self):
        primary, backup = FakeEngine("local-f5"), FakeEngine("google")
        chain = EngineChain([primary, backup])

        result = await chain.synth("สวัสดี", BOOMMER)

        self.assertEqual(result.engine, "local-f5")
        self.assertFalse(result.fell_back)
        self.assertEqual(backup.calls, [])

    async def test_unavailable_primary_falls_back_and_notifies(self):
        """หัวใจของ design — fallback ต้อง **ไม่เงียบ** ผู้ฟังต้องรู้ว่ากำลังฟังเสียงใคร"""
        primary = FakeEngine("local-f5", unavailable=True)
        backup = FakeEngine("google")
        seen = []
        chain = EngineChain([primary, backup], on_fallback=lambda n, r: seen.append((n, r)))

        result = await chain.synth("สวัสดี", BOOMMER)

        self.assertEqual(result.engine, "google")
        self.assertTrue(result.fell_back)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "google")
        self.assertIn("local-f5", seen[0][1])

    async def test_hard_failure_does_not_fall_back(self):
        """คำขอผิด (4xx) ไม่ใช่ engine ล่ม — ห้ามเผาโควตา Google ซ้ำด้วยคำขอเดิม"""
        primary = FakeEngine("local-f5", hard_fail=True)
        backup = FakeEngine("google")
        chain = EngineChain([primary, backup])

        with self.assertRaises(RuntimeError):
            await chain.synth("สวัสดี", BOOMMER)
        self.assertEqual(backup.calls, [])

    async def test_all_engines_unavailable_raises_with_all_reasons(self):
        chain = EngineChain([FakeEngine("local-f5", unavailable=True),
                             FakeEngine("google", unavailable=True)])

        with self.assertRaises(RuntimeError) as ctx:
            await chain.synth("สวัสดี", BOOMMER)

        self.assertIn("local-f5", str(ctx.exception))
        self.assertIn("google", str(ctx.exception))

    async def test_empty_chain_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            EngineChain([])

    async def test_fallback_notice_fires_once_per_chunk_not_per_engine(self):
        chain_engines = [FakeEngine("local-f5", unavailable=True),
                         FakeEngine("mid", unavailable=True),
                         FakeEngine("google")]
        seen = []
        chain = EngineChain(chain_engines, on_fallback=lambda n, r: seen.append(n))

        await chain.synth("สวัสดี", BOOMMER)

        self.assertEqual(seen, ["google"])


class VoiceSpecTests(unittest.TestCase):
    def test_voice_spec_is_hashable_for_use_as_map_value(self):
        self.assertEqual(BOOMMER.agent, "Boommer")
        self.assertEqual(hash(BOOMMER), hash(BOOMMER))

    def test_per_agent_map_can_mix_engines(self):
        """ทางเลือก hybrid ต้องใส่ map ได้เลย ไม่ต้องแก้ design"""
        voice_map = {"Boommer": BOOMMER, "AgentA": AGENT_A}
        self.assertEqual(voice_map["Boommer"].engine, "local")
        self.assertEqual(voice_map["AgentA"].engine, "google")


if __name__ == "__main__":
    unittest.main()
