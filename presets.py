"""PRESET — ชุด config การอ่านออกเสียง ผูกกับ "เสียง" ไม่ใช่ผูกกับ "ความจริง"

## ทำไมต้องมีไฟล์นี้ (ruling ของ Boommer ผ่าน AgentA 2026-07-30)

A/B รอบสามให้ผลว่า config ของตำราสำนัก Creator (`light`, `speed 0.9`) **แพ้** config ของเรา
บนเสียงพี่ชาย (`cand-34.50`) — แต่ **นั่นไม่ได้แปลว่าตำราผิด**

    "Creator ไม่ผิด — เสียงแต่ละตัวต้องจูนเฉพาะ"

⇒ config ที่ชนะบนเสียงหนึ่ง **ไม่ใช่ความจริงทั่วไป** มันคือ *ตัวจูนของเสียงนั้น*
⇒ เก็บ config ของตำราไว้เป็น **preset อ้างอิงถาวร ห้ามทิ้ง** (Principle 1 — Nothing is Deleted)
⇒ พิธีรับเสียง ref ใหม่ = **ยิงทุก preset เป็นแบตเตอรี่มาตรฐาน** แล้วให้หู Boommer ตัดสิน
   ไม่ใช่เอา preset ที่ชนะครั้งก่อนไปใช้เลย

## กฎที่ไฟล์นี้บังคับ

ทุก preset ต้องแบก **ขอบเขตของหลักฐานตัวเอง** (`evidence`) ไปด้วย — ไม่ใช่แค่ค่าตัวเลข
เพราะ failure mode ประจำตำแหน่งของ dock คือ **ประโยคที่กว้างกว่าสิ่งที่วัดจริง**
`evidence` ทำให้ "อ้างอิงจากตำรา" กับ "ผ่านหูบนเครื่องนี้" อยู่คนละช่อง กลืนกันไม่ได้
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass(frozen=True)
class Preset:
    """ค่าที่ส่งผลต่อสิ่งที่ผู้ฟังได้ยิน — ทุกตัวต้องอยู่ที่นี่ ไม่กระจายไปเป็น env var ลับ"""

    name: str
    # --- ตัวแปรที่ A/B รอบสามทดสอบ ---
    keep_space_in_chunk: bool     # True = space เดิมคงอยู่ (F5 อ่านเป็น pause ที่มันเดาเอง)
    speed: float                  # ตำรา §9: เป็น register ไม่ใช่ playback rate
    # --- ตัวแปรที่ตรงกันทุก preset (ตำราและเราได้ค่าเดียวกันโดยอิสระ) ---
    cfg: float = 2.0              # ตำรา §4: 2.5/3.0 ลด speaker similarity
    step: int = 32
    gap_para: float = 0.55        # ตำรา `PEND`
    gap_mid: float = 0.22         # ตำรา `PMID`
    max_chunk: int = 90           # หน่วย = ตัวอักษร (ตำราใช้ token/พยางค์ — ดู unmapped)
    # --- ที่มาและขอบเขตของหลักฐาน ---
    source: str = ""
    evidence: str = ""
    unmapped: tuple = field(default_factory=tuple)

    def as_dict(self):
        return asdict(self)


# หน่วยการหั่นของตำราเป็น token/พยางค์ ของเราเป็นตัวอักษร ⇒ ฟิลด์พวกนี้ **แปลงตรงๆ ไม่ได้**
# เขียนไว้เพื่อไม่ให้ใครคิดว่า preset ตำราถูกถอดมาครบ — มันไม่ครบ และนั่นคือข้อจำกัดที่รู้ตัว
_CREATOR_UNMAPPED = (
    "SYL_GATE=8 (ไม่ break จนสะสม >=8 พยางค์) — เราหั่นด้วยตัวอักษร ไม่มีตัวนับพยางค์",
    "connector break เมื่อสะสม >=4 token",
    "v3_breath: บังคับตัดที่ token ที่ 11",
    "NP-guard (POS tag orchid_ud) ห้ามตัด noun->adjective",
    "expand_maiyamok / normalize (ZWSP) — ยังไม่มีใน pipeline เรา",
)

PRESETS = {
    # ─────────── ตัวจูนของเสียงพี่ชาย — ผ่านหู Boommer แล้ว ───────────
    "brother-voice": Preset(
        name="brother-voice",
        keep_space_in_chunk=True,
        speed=1.0,
        source="AgentP วัดเอง + A/B รอบสาม (arm E)",
        evidence=(
            "ผ่านหู Boommer 2026-07-30 — ชนะ arm F/G/H บนบทความ FIFA ท่อนต้น 9 ก้อน "
            "ref cand-34.50 · n=1 ต่อ arm · ยังไม่ทดสอบกับบทความอื่น/ref อื่น"
        ),
    ),
    # ─────────── preset อ้างอิงจากตำรา — ห้ามลบ ───────────
    "creator-light": Preset(
        name="creator-light",
        keep_space_in_chunk=False,
        speed=1.0,
        source="ตำราสำนัก Creator บท 8–9 · variant v4_light (default ของเขา)",
        evidence=(
            "ตำราระบุว่าชนะ eval **ของเขา** บนสไตล์ประโยคสั้น-กลางแบบพูดคุยของ Nat — "
            "ไม่ใช่ผลบนเครื่องนี้ · แพ้ arm E ตอน A/B รอบสามบนเสียงพี่ชาย (long-form)"
        ),
        unmapped=_CREATOR_UNMAPPED,
    ),
    "creator-light-slow": Preset(
        name="creator-light-slow",
        keep_space_in_chunk=False,
        speed=0.9,
        source="ตำราสำนัก Creator บท 9 · variant v6_light_slow",
        evidence=(
            "ตำราแนะ speed 0.9 สำหรับ narration (1.0 เร็วเกินเมื่อ ref พูดเร็วอยู่แล้ว) — "
            "ไม่ใช่ผลบนเครื่องนี้ · แพ้ arm E ตอน A/B รอบสามบนเสียงพี่ชาย"
        ),
        unmapped=_CREATOR_UNMAPPED,
    ),
}

# preset ที่ใช้จริงตอนนี้ — เปลี่ยนค่านี้ = เปลี่ยนสิ่งที่ผู้ฟังได้ยิน ⇒ ต้องผ่านหู Boommer ก่อน
DEFAULT_PRESET = "brother-voice"

# แบตเตอรี่มาตรฐานสำหรับ **พิธีรับเสียง ref ใหม่** — ยิงทุกตัวแล้วให้หูตัดสิน
# ห้ามข้ามด้วยเหตุผลว่า "ครั้งก่อน brother-voice ชนะ" — ครั้งก่อนคือเสียงคนละตัว
ONBOARDING_BATTERY = ("brother-voice", "creator-light", "creator-light-slow")


def get(name: Optional[str] = None) -> Preset:
    """คืน preset ตามชื่อ — **ชื่อที่ไม่รู้จักต้องล้ม ไม่ใช่ fallback เงียบ**

    allowlist ไม่ใช่ denylist (AgentZ) — ถ้า typo แล้วเงียบๆ ใช้ default
    เราจะได้เสียงที่ไม่มีใครสั่ง และไม่มีใครรู้ว่าเกิดอะไรขึ้น
    """
    # `name or DEFAULT_PRESET` ผิด — `""` เป็น falsy ⇒ ค่าว่างจะกลายเป็น default **เงียบๆ**
    # ซึ่งคือพฤติกรรมที่ docstring ข้างบนบอกว่าห้ามมี · test จับได้ตอนรันครั้งแรก
    key = DEFAULT_PRESET if name is None else name
    if key not in PRESETS:
        raise KeyError(
            f"ไม่รู้จัก preset {key!r} — ที่มีคือ {sorted(PRESETS)} "
            f"(ถ้าเพิ่ม preset ใหม่ ต้องเขียน source + evidence ด้วย)"
        )
    return PRESETS[key]


def prepare_chunk(chunk: str, preset: Preset) -> str:
    """ใช้ policy เรื่อง space ของ preset กับก้อนหนึ่ง — จุดเดียวที่ตัดสินใจเรื่องนี้"""
    return chunk if preset.keep_space_in_chunk else chunk.replace(" ", "")
