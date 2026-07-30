"""ด่านของ presets ต้องหยุดของที่ควรถูกปฏิเสธได้จริง — ไม่ใช่มีป้าย (AgentB ข้อ 4)"""
import unittest

import presets


class TestPresetRegistry(unittest.TestCase):
    def test_default_is_the_one_that_passed_boommer_ear(self):
        p = presets.get()
        self.assertEqual(p.name, "brother-voice")
        self.assertTrue(p.keep_space_in_chunk)   # arm E ชนะ = คง space
        self.assertEqual(p.speed, 1.0)           # arm E ชนะ = speed 1.0

    def test_unknown_preset_raises_not_silently_defaults(self):
        # ถ้า typo แล้วเงียบๆ ใช้ default => ได้เสียงที่ไม่มีใครสั่ง
        with self.assertRaises(KeyError):
            presets.get("brothervoice")
        with self.assertRaises(KeyError):
            presets.get("")            # ค่าว่างต้องไม่กลายเป็น default

    def test_creator_presets_are_never_removed(self):
        # Nothing is Deleted — preset อ้างอิงต้องอยู่ ห้ามหายไปเพราะ "มันแพ้"
        for name in ("creator-light", "creator-light-slow"):
            self.assertIn(name, presets.PRESETS)

    def test_every_preset_carries_its_own_evidence_scope(self):
        # ค่าตัวเลขที่ไม่มีขอบเขตติดมา คือค่าที่พร้อมถูกอ้างเกินจริง
        for name, p in presets.PRESETS.items():
            self.assertTrue(p.source.strip(), f"{name} ไม่มี source")
            self.assertTrue(p.evidence.strip(), f"{name} ไม่มี evidence")

    def test_creator_presets_declare_what_could_not_be_mapped(self):
        # ต้องไม่มีใครคิดว่า preset ตำราถูกถอดมาครบ
        self.assertTrue(presets.PRESETS["creator-light"].unmapped)
        self.assertTrue(presets.PRESETS["creator-light-slow"].unmapped)

    def test_shared_values_agree_across_presets(self):
        # ตำราและเราได้ 0.55/0.22/cfg 2.0 มาโดยอิสระ — ถ้าวันหนึ่งไม่ตรง ต้องรู้ตัว
        for p in presets.PRESETS.values():
            self.assertEqual((p.cfg, p.gap_para, p.gap_mid), (2.0, 0.55, 0.22))

    def test_onboarding_battery_covers_all_presets(self):
        # พิธีรับเสียงใหม่ต้องยิงครบ ห้ามข้ามเพราะครั้งก่อนตัวไหนชนะ
        self.assertEqual(set(presets.ONBOARDING_BATTERY), set(presets.PRESETS))

    def test_preset_is_frozen(self):
        with self.assertRaises(Exception):
            presets.get().speed = 0.5


class TestPrepareChunk(unittest.TestCase):
    TXT = "สตูดิโอ ใหญ่มาก จริงๆ"

    def test_keep_space_preset_leaves_text_untouched(self):
        self.assertEqual(presets.prepare_chunk(self.TXT, presets.get("brother-voice")), self.TXT)

    def test_strip_space_preset_removes_every_space(self):
        out = presets.prepare_chunk(self.TXT, presets.get("creator-light"))
        self.assertNotIn(" ", out)
        self.assertEqual(out, "สตูดิโอใหญ่มากจริงๆ")

    def test_strip_does_not_change_anything_but_spaces(self):
        a = presets.prepare_chunk(self.TXT, presets.get("creator-light"))
        self.assertEqual(a, self.TXT.replace(" ", ""))


class TestAiFixedPolicy(unittest.TestCase):
    def test_ai_reads_as_full_thai_word(self):
        import thai_text
        # ตำรา §8.4 fixed policy · รับโดย Boommer 2026-07-30
        # ⚠️ รับ **โดยไม่มีหลักฐานเทียบ** — A/B รอบสามใส่คำนี้ทั้ง 4 clip
        #    จึงไม่มี arm ไหนอ่าน "เอไอ" ให้เทียบ ห้ามเขียนว่าผ่าน A/B แล้ว
        self.assertEqual(thai_text.DICT["AI"], "ปัญญาประดิษฐ์")

    def test_compound_ai_terms_unchanged(self):
        import thai_text
        # ตำราไม่ได้พูดถึงคำประกอบ — คงไว้ และ translit ต้องเลือกยาวก่อนสั้น
        self.assertEqual(thai_text.DICT["AI safety"], "เอไอ เซฟตี้")
        self.assertIn("โอเพนเอไอ", thai_text.translit("OpenAI"))

    def test_standalone_ai_in_sentence(self):
        import thai_text
        out = thai_text.translit("ยิ่งถ้า AI คือการเปลี่ยนแปลง")
        self.assertIn("ปัญญาประดิษฐ์", out)
        self.assertEqual(thai_text.residual_latin(out), [])


if __name__ == "__main__":
    unittest.main()
