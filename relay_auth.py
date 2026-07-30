"""ด่านของ relay endpoint — ตรวจสิทธิ์ก่อนยอมให้ใครยิงไฟล์เสียงเข้าแชท

## ทำไมต้องมีไฟล์นี้

สถาปัตยกรรม B (วัดแล้ว 2026-07-30): pc-office ส่งไฟล์ไป Render (6.36 MB/s) แล้ว
Render ส่งต่อ Telegram (2.36 MB/s) แทนที่จะส่งตรงจากบ้าน (13–33 KB/s)
⇒ **<2 วินาที แทน 178 วินาที**

แต่ endpoint ที่รับไฟล์จากอินเทอร์เน็ตแล้วยิงเข้าแชท Boommer ถ้าปล่อยเปิด
**ใครก็ส่งไฟล์ให้ bot ยิงเข้าแชทได้** ⇒ ไฟล์นี้คือด่าน ไม่ใช่ของประกอบ

## กติกาการออกแบบที่ยึด

1. **Allowlist ไม่ใช่ denylist** (AgentZ) — `ALLOWED_CHAT_IDS` ว่าง = **ปฏิเสธทุกคน**
   ⚠️ ตรงนี้ **ต่างจาก `bot.py`** โดยเจตนา: ใน `bot.py` allowlist ว่าง = ให้ทุกคน
   (เพราะคนที่คุยกับ bot ต้องรู้ชื่อ bot ก่อน) แต่ relay รับจากอินเทอร์เน็ตทั้งใบ
   ⇒ ค่า default ที่ปลอดภัยกลับด้านกัน · **ความต่างนี้มีเทสต์คุมทั้งสองฝั่ง**
2. **ไม่มี secret = ปิด endpoint** (fail closed) ไม่ใช่ปิดการตรวจ
3. **ด่านต้องหยุดการกระทำได้จริง** (AgentB ข้อ 4) — ฟังก์ชันนี้ไม่ส่งอะไรเลย
   มันคืน `Decision` และ **ผู้เรียกต้องส่งเมื่อ `ok` เท่านั้น** (`check && do`)
4. **ทุกเทสต์ปฏิเสธต้องแดงด้วยเหตุผลที่ตั้งใจ** ⇒ `Decision.reason` เป็น slug
   ที่เทสต์ assert ได้ · ถ้าเช็ค "ลายเซ็นผิด" แล้วมันแดงเพราะ `missing_header`
   เท่ากับด่านนั้นยังไม่ได้ถูกทดสอบ (บทเรียน `PATH=/bin` 2026-07-30 เช้า)

## ลำดับการตรวจ — ลำดับคือส่วนหนึ่งของความปลอดภัย

    no_secret -> ตรวจรูปคำขอ -> ts skew -> **ลายเซ็น** -> nonce -> allowlist

`nonce` ตรวจ **หลัง** ลายเซ็นเสมอ · ถ้าตรวจก่อน คนที่ไม่มี secret จะ**เผา nonce
ของคนที่มี secret ได้** (poison cache = DoS ผู้ส่งที่ถูกต้อง)
`allowlist` ตรวจหลังลายเซ็น เพราะลายเซ็นครอบ `chat_id` อยู่แล้ว ⇒ คนไม่มี secret
ได้คำตอบ `bad_signature` เสมอ ไม่รู้ว่า chat ไหนอยู่ใน allowlist
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Mapping, Optional

# ---------- ค่าคงที่ของด่าน ----------
HDR_TS = "x-agentp-timestamp"
HDR_NONCE = "x-agentp-nonce"
HDR_CHAT = "x-agentp-chat-id"
HDR_SIG = "x-agentp-signature"

MAX_SKEW_SECONDS = 300          # ±5 นาที — กว้างพอสำหรับ clock drift, แคบพอให้ nonce cache ไม่บวม
MAX_BODY_BYTES = 50 * 1024 * 1024   # เพดาน upload ของ bot API — ใหญ่กว่านี้ Telegram ปฏิเสธเองอยู่แล้ว
MIN_NONCE_LEN = 16              # nonce สั้นๆ เดาได้ ⇒ replay window ที่ยังไม่หมดอายุถูกชนได้
MIN_SECRET_LEN = 32             # นโยบายที่ AgentP ตั้งเอง ไม่ใช่ใบสั่ง — ปรับได้ ดูหมายเหตุใน verify()
MAX_NONCE_ENTRIES = 20_000      # กันหน่วยความจำบวม — เต็มแล้ว fail closed ไม่ใช่ล้างทิ้งแล้วปล่อยผ่าน


@dataclass(frozen=True)
class Decision:
    """ผลของด่าน — `ok=False` ต้องแปลว่า **ไม่มีการส่งเกิดขึ้น**"""

    ok: bool
    reason: str = ""            # slug ที่เทสต์ assert ได้ ว่าแดงด้วยเหตุผลที่ตั้งใจ
    status: int = 401
    chat_id: Optional[int] = None
    detail: str = ""            # ข้อความอ่านง่าย — ห้ามใส่ secret / ลายเซ็นที่คาดไว้

    def __bool__(self) -> bool:  # ให้ `if verify(...):` อ่านได้ตรงๆ
        return self.ok


class NonceCache:
    """จำ nonce ที่ใช้แล้วภายในหน้าต่าง skew — กัน replay ของคำขอที่ลายเซ็นถูก

    ไม่ใช้ TTL cache สำเร็จรูป เพราะ deploy นี้มี process เดียวและอยากให้พฤติกรรม
    ตอน "เต็ม" ชัดเจน: **ปฏิเสธ ไม่ใช่ล้างแล้วปล่อยผ่าน**
    """

    def __init__(self, window_seconds: float = MAX_SKEW_SECONDS, max_entries: int = MAX_NONCE_ENTRIES):
        self._seen: dict[str, float] = {}
        self._window = window_seconds
        self._max = max_entries

    def purge(self, now: float) -> None:
        cutoff = now - self._window * 2      # x2 เพราะ ts อาจล้ำหน้าได้ถึง +window
        stale = [n for n, ts in self._seen.items() if ts < cutoff]
        for n in stale:
            del self._seen[n]

    def seen(self, nonce: str) -> bool:
        return nonce in self._seen

    def remember(self, nonce: str, ts: float) -> None:
        self._seen[nonce] = ts

    def is_full(self) -> bool:
        return len(self._seen) >= self._max

    def __len__(self) -> int:
        return len(self._seen)


def signing_string(ts: str, nonce: str, chat_id: str, body: bytes) -> str:
    """ข้อความที่ถูกเซ็น — **ผู้ส่งกับผู้รับต้องประกอบเหมือนกันเป๊ะ**

    ใช้ sha256 ของ body ไม่ใช่ body ตรงๆ เพื่อไม่ต้องถือไฟล์ 50 MB ไว้ใน HMAC
    และเพื่อให้ผู้ส่งคำนวณลายเซ็นได้ก่อนเริ่มสตรีม
    """
    return "\n".join([ts, nonce, chat_id, hashlib.sha256(body).hexdigest()])


def expected_signature(secret: str, ts: str, nonce: str, chat_id: str, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        signing_string(ts, nonce, chat_id, body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _get(headers: Mapping[str, str], name: str) -> str:
    """อ่าน header แบบไม่สนตัวพิมพ์ — HTTP header case-insensitive ตาม RFC"""
    for k, v in headers.items():
        if k.lower() == name:
            return v.strip()
    return ""


def verify(
    headers: Mapping[str, str],
    body: bytes,
    *,
    secret: Optional[str],
    allowed_chat_ids: frozenset[int],
    nonce_cache: NonceCache,
    now: Optional[float] = None,
) -> Decision:
    """ตรวจคำขอหนึ่งใบ — **ไม่ส่งอะไร ไม่แก้ state ใดๆ นอกจาก nonce_cache ตอนผ่าน**

    `now` รับเข้ามาเพื่อให้เทสต์ควบคุมเวลาได้ (ไม่งั้นเทสต์ ts skew จะ flaky)
    """
    if now is None:
        now = time.time()

    # ── 1. ไม่มี secret = ปิด endpoint (fail closed) ──────────────────────────
    # ห้ามแปลว่า "ไม่ต้องตรวจ" · 503 เพราะเป็นความผิดของ config ฝั่งเรา ไม่ใช่ของผู้เรียก
    #
    # 🔴 `if not secret:` เพียวๆ **ไม่พอ** — `"   "` เป็น truthy ⇒ endpoint จะเปิด
    # ด้วยกุญแจขยะ · เทสต์ `test_no_secret_closes_endpoint_not_the_check` จับได้
    # ก่อน commit ครั้งแรก · เป็นตระกูลเดียวกับ `name or DEFAULT` ของ `presets.get("")`
    # (ค่าที่ falsy/blank ถูกกลืน) — ผิดซ้ำแบบเดิม แต่รอบนี้ด่านจับได้เอง
    clean_secret = (secret or "").strip()
    if not clean_secret:
        return Decision(False, "no_secret", 503, detail="relay ปิดอยู่ — ไม่ได้ตั้ง secret")
    if len(clean_secret) < MIN_SECRET_LEN:
        # กุญแจสั้นเดาได้ ⇒ ด่านมีอยู่แต่ไม่กัน · ปิดไว้ดีกว่าเปิดครึ่งใบ
        # ⚠️ นี่เป็นนโยบายที่หนูตั้งเอง (ไม่ได้มาจากใบสั่ง) — Boommer/พี่ A ปรับเลขได้
        return Decision(False, "weak_secret", 503,
                        detail=f"secret สั้นกว่า {MIN_SECRET_LEN} ตัวอักษร")
    secret = clean_secret

    # ── 2. รูปคำขอ ──────────────────────────────────────────────────────────
    ts_raw = _get(headers, HDR_TS)
    nonce = _get(headers, HDR_NONCE)
    chat_raw = _get(headers, HDR_CHAT)
    sig = _get(headers, HDR_SIG)

    for name, value in ((HDR_TS, ts_raw), (HDR_NONCE, nonce), (HDR_CHAT, chat_raw), (HDR_SIG, sig)):
        if not value:
            return Decision(False, "missing_header", 400, detail=f"ขาด header {name}")

    if len(nonce) < MIN_NONCE_LEN:
        return Decision(False, "nonce_too_short", 400,
                        detail=f"nonce ต้องยาว >= {MIN_NONCE_LEN} ตัวอักษร")
    if not body:
        return Decision(False, "empty_body", 400, detail="ไม่มีไฟล์มาด้วย")
    if len(body) > MAX_BODY_BYTES:
        return Decision(False, "body_too_large", 413,
                        detail=f"{len(body)} ไบต์ เกินเพดาน {MAX_BODY_BYTES}")

    try:
        ts = float(ts_raw)
    except ValueError:
        return Decision(False, "malformed_timestamp", 400, detail="timestamp ไม่ใช่ตัวเลข")
    try:
        chat_id = int(chat_raw)
    except ValueError:
        return Decision(False, "malformed_chat_id", 400, detail="chat_id ไม่ใช่จำนวนเต็ม")

    # ── 3. ts skew ──────────────────────────────────────────────────────────
    # ตรวจก่อนลายเซ็นได้ เพราะ ts เป็นของผู้เรียกเอง ไม่รั่วอะไรของเรา
    # และมันจำกัดหน้าต่างที่ nonce cache ต้องจำ
    if abs(now - ts) > MAX_SKEW_SECONDS:
        return Decision(False, "stale_timestamp", 401,
                        detail=f"ต่างจากเวลาเซิร์ฟเวอร์ {abs(now - ts):.0f} วิ (เพดาน {MAX_SKEW_SECONDS})")

    # ── 4. ลายเซ็น — ก่อน nonce เสมอ ────────────────────────────────────────
    expect = expected_signature(secret, ts_raw, nonce, chat_raw, body)
    if not hmac.compare_digest(expect, sig):     # เทียบเวลาคงที่ ไม่ใช่ `==`
        return Decision(False, "bad_signature", 401, detail="ลายเซ็นไม่ตรง")

    # ── 5. nonce replay — หลังลายเซ็นผ่านแล้วเท่านั้น ────────────────────────
    nonce_cache.purge(now)
    if nonce_cache.seen(nonce):
        return Decision(False, "replayed_nonce", 409, detail="nonce นี้ใช้ไปแล้ว")
    if nonce_cache.is_full():
        # เต็มแล้วต้องปฏิเสธ ไม่ใช่ล้างทิ้งแล้วปล่อยผ่าน — ล้างทิ้งคือเปิดช่อง replay
        return Decision(False, "nonce_cache_full", 503, detail="nonce cache เต็ม")

    # ── 6. allowlist ────────────────────────────────────────────────────────
    # ว่าง = ปฏิเสธทุกคน (ต่างจาก bot.py โดยเจตนา — ดู docstring ข้างบน)
    if chat_id not in allowed_chat_ids:
        return Decision(False, "chat_not_allowed", 403, chat_id=chat_id,
                        detail="chat_id ไม่อยู่ใน allowlist ของ relay")

    # ผ่านครบ — จำ nonce ตอนนี้ (ไม่ใช่ก่อนหน้า) เพื่อไม่ให้คำขอที่ถูกปฏิเสธเผา nonce
    nonce_cache.remember(nonce, ts)
    return Decision(True, "ok", 200, chat_id=chat_id)


def sign_request(secret: str, chat_id: int, body: bytes, *, nonce: str,
                 ts: Optional[float] = None) -> dict[str, str]:
    """ฝั่งผู้ส่ง (pc-office) ประกอบ header — **อยู่ไฟล์เดียวกับผู้ตรวจโดยเจตนา**

    ถ้าแยกไฟล์ วันหนึ่งสองฝั่งจะประกอบ signing string ไม่เหมือนกันแล้วไม่มีใครรู้
    จนกว่าจะมีคนยิงจริง · nonce ต้องส่งเข้ามา ไม่สุ่มในนี้ เพื่อให้เทสต์ทำ replay ได้
    """
    ts_raw = repr(float(time.time() if ts is None else ts))
    chat_raw = str(int(chat_id))
    return {
        HDR_TS: ts_raw,
        HDR_NONCE: nonce,
        HDR_CHAT: chat_raw,
        HDR_SIG: expected_signature(secret, ts_raw, nonce, chat_raw, body),
    }
