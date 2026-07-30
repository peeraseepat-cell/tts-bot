"""
บทที่ 8 — translit ศัพท์/ชื่ออังกฤษเป็นไทยก่อน tokenize

เหตุผลที่ต้องทำก่อน ไม่ใช่ปล่อยให้โมเดลเดาเอง:
F5 เป็น TTS ไทย — token อังกฤษที่หลุดเข้าไปจะถูกอ่านแบบสะกดทีละตัว
หรือถูกข้ามไปเงียบๆ ทั้งสองแบบพังทั้งคู่ และแบบที่สอง **พังแบบเงียบ**
คือฟังแล้วลื่นแต่เนื้อความหาย ซึ่งจับได้เฉพาะตอน STT round-trip

กติกาของ dict ข้างล่าง:
- เรียงแทนที่ **ยาวก่อนสั้น** เสมอ ("Sam Altman" ต้องชนะ "Sam")
- match แบบมีขอบเขตคำ ไม่งั้น "AI" จะไปกิน "AI" ใน "OpenAI" ที่แทนไปแล้ว
- ตัวเลขปีแปลงเป็นคำอ่านไทย ไม่ปล่อยเป็นเลขอารบิก
  (บทเรียน CER เมื่อวาน: เลขอารบิก↔เลขคำ คือจุดที่ metric กับหูไม่ตรงกัน)
"""
import re

# ยาวก่อนสั้น — dict นี้ถูก sort ด้วย len ตอนใช้จริงอยู่แล้ว แต่จัดกลุ่มให้คนอ่านง่าย
DICT = {
    # ชื่อคน / องค์กร
    "Sam Altman": "แซม อัลท์แมน",
    "Paul Graham": "พอล เกรแฮม",
    "Garry Tan": "แกรี่ แทน",
    "Greg Brockman": "เกร็ก บร็อกแมน",
    "Y Combinator": "วาย คอมบิเนเตอร์",
    "Hugging Face": "ฮักกิ้ง เฟซ",
    "Startup School": "สตาร์ทอัพ สคูล",
    "App Store": "แอปสโตร์",
    "San Francisco": "แซนแฟรนซิสโก",
    "Palo Alto": "พาโล อัลโต",
    "Bay Area": "เบย์ แอเรีย",
    "Combinator": "คอมบิเนเตอร์",
    "Brockman": "บร็อกแมน",
    "Cambridge": "เคมบริดจ์",
    "Facebook": "เฟซบุ๊ก",
    "ChatGPT": "แชตจีพีที",
    "Altman": "อัลท์แมน",
    "Graham": "เกรแฮม",
    "OpenAI": "โอเพนเอไอ",
    "Google": "กูเกิล",
    "Stripe": "สไตรพ์",
    "iPhone": "ไอโฟน",
    "Codex": "โคเด็กซ์",
    "Loopt": "ลูปต์",
    "Garry": "แกรี่",
    "Greg": "เกร็ก",
    "Paul": "พอล",
    "Face": "เฟซ",
    "Alto": "อัลโต",
    "Sam": "แซม",
    "Tan": "แทน",
    "YC": "วายซี",

    # ศัพท์เทคนิค — วลีก่อนคำเดี่ยว
    "coding agent": "โค้ดดิ้ง เอเจนต์",
    "frontier model": "ฟรอนเทียร์ โมเดล",
    "frontier lab": "ฟรอนเทียร์ แล็บ",
    "network effect": "เน็ตเวิร์ก เอฟเฟกต์",
    "Brownian motion": "บราวเนียน โมชัน",
    "deep learning": "ดีปเลิร์นนิง",
    "internet boom": "อินเทอร์เน็ต บูม",
    "alpha leak": "อัลฟ่า ลีก",
    "AI safety": "เอไอ เซฟตี้",
    "AI winter": "เอไอ วินเทอร์",
    "hard tech": "ฮาร์ดเทค",
    "co-founder": "โคฟาวน์เดอร์",
    "superpower": "ซูเปอร์พาวเวอร์",
    "exponential": "เอกซ์โพเนนเชียล",
    "intelligence": "อินเทลลิเจนซ์",
    "ecosystem": "อีโคซิสเต็ม",
    "alignment": "อะไลน์เมนต์",
    "inference": "อินเฟอเรนซ์",
    "internet": "อินเทอร์เน็ต",
    "reasoning": "รีซันนิง",
    "frontier": "ฟรอนเทียร์",
    "security": "ซีเคียวริตี้",
    "startup": "สตาร์ทอัพ",
    "Startup": "สตาร์ทอัพ",
    "failure": "เฟลเลอร์",
    "compute": "คอมพิวต์",
    "network": "เน็ตเวิร์ก",
    "winter": "วินเทอร์",
    "coding": "โค้ดดิ้ง",
    "safety": "เซฟตี้",
    "School": "สคูล",
    "effect": "เอฟเฟกต์",
    "prompt": "พรอมต์",
    "motion": "โมชัน",
    "Store": "สโตร์",
    "batch": "แบตช์",
    "alpha": "อัลฟ่า",
    "agent": "เอเจนต์",
    "model": "โมเดล",
    "token": "โทเคน",
    "troll": "โทรล",
    "tech": "เทค",
    "meme": "มีม",
    "moat": "โมท",
    "boom": "บูม",
    "leak": "ลีก",
    "lab": "แล็บ",
    "API": "เอพีไอ",
    "AGI": "เอจีไอ",
    "RAM": "แรม",
    "AI": "เอไอ",
}

# ─────────────────────────────────────────────────────────────────────────────
# 2026-07-30: ศัพท์ที่ฝูง oracle ใช้จริง — เพิ่มหลังวัดแล้วว่า dict เดิมไม่ครอบเลย
#
# ใบเสร็จ: ข้อความ "ผม merge PR หนึ่งร้อยยี่สิบสองเข้า main แล้ว รอ deploy บน Vercel"
# ผ่าน translit เดิม → ละตินเหลือ 5 คำเท่าเดิม → F5 กลืนหาย
# STT ถอดได้ "ผม 122 comment แล้ว รอ The Poe บน Vercel" (merge/PR หาย · main→comment)
# ดู ψ/memory/learnings/2026-07-30_latin-residue-silent-drop.md
#
# ทำไมต้องแยกเป็น dict ที่สอง แทนที่จะยัดลง DICT:
# ศัพท์ dev พิมพ์มาได้หลายเคส (merge / Merge / MERGE) แต่ชื่อเฉพาะใน DICT
# ต้องคง case-sensitive ("Face" ใน Hugging Face ≠ "face")
# ⇒ ชุดนี้ match แบบ **ไม่สนตัวพิมพ์** เพื่อไม่ต้องใส่ 3 รูปต่อคำ (ซึ่งจะ drift แน่)
# ─────────────────────────────────────────────────────────────────────────────
DICT_CI = {
    # git / เวิร์กโฟลว์ — วลีก่อนคำเดี่ยว
    "pull request": "พูลรีเควสต์",
    "merge conflict": "เมิร์จ คอนฟลิกต์",
    "force push": "ฟอร์ซพุช",
    "worktree": "เวิร์กทรี",
    "rollback": "โรลแบ็ก",
    "rebase": "รีเบส",
    "commit": "คอมมิต",
    "branch": "แบรนช์",
    "merge": "เมิร์จ",
    "clone": "โคลน",
    "diff": "ดิฟ",
    "push": "พุช",
    "pull": "พูล",
    "repo": "รีโป",
    "git": "กิต",
    "PR": "พีอาร์",
    "SHA": "ชา",

    # deploy / รันไทม์
    "staging": "สเตจจิง",
    "production": "โปรดักชัน",
    "deploy": "ดีพลอย",
    "release": "รีลีส",
    "build": "บิลด์",
    "server": "เซิร์ฟเวอร์",
    "client": "ไคลเอนต์",
    "endpoint": "เอนด์พอยต์",
    "database": "ดาตาเบส",
    "config": "คอนฟิก",
    "script": "สคริปต์",
    "cache": "แคช",
    "query": "เควรี",
    "queue": "คิว",
    "prod": "โปรด",
    "CI": "ซีไอ",

    # งานตรวจ / คุณภาพ
    "benchmark": "เบนช์มาร์ก",
    "throughput": "ทรูพุต",
    "latency": "เลเทนซี",
    "review": "รีวิว",
    "issue": "อิสชู",
    "test": "เทสต์",
    "bug": "บัก",
    "fix": "ฟิกซ์",
    "log": "ล็อก",

    # ฮาร์ดแวร์ / เครื่องมือที่ dock พูดถึงทุกวัน
    "TypeScript": "ไทป์สคริปต์",
    "JavaScript": "จาวาสคริปต์",
    "Supabase": "ซูปาเบส",
    "Telegram": "เทเลแกรม",
    "GitHub": "กิตฮับ",
    "Vercel": "เวอร์เซล",
    "Docker": "ดอกเกอร์",
    "Python": "ไพทอน",
    "VRAM": "วีแรม",
    "oracle": "ออราเคิล",
    "GPU": "จีพียู",
    "CPU": "ซีพียู",
    "npm": "เอ็นพีเอ็ม",
    "main": "เมน",

    # หน่วย — โผล่ทุกครั้งที่ dock รายงานตัวเลข ซึ่งคือเกือบทุกใบ
    "req/s": "รีเควสต์ต่อวินาที",
    "GB": "กิกะไบต์",
    "MB": "เมกะไบต์",
    "KB": "กิโลไบต์",
    "TB": "เทระไบต์",
    "ms": "มิลลิวินาที",
    "req": "รีเควสต์",
}

# ปีและตัวเลขที่อ่านต่างจาก default ของ pythainlp — เก็บเป็น override
# (ตรวจแล้ว 2026/2005/1998/1999/20 pythainlp ให้ตรงกันทุกตัว ⇒ ชุดนี้ว่างได้
#  แต่ไม่ลบทิ้ง เพราะช่องนี้คือที่สำหรับเคสที่ lib อ่านไม่ตรงกับที่คนพูด)
NUM = {}

_LAT = re.compile(r"[A-Za-z]")
# ห้ามแตะเลขที่มีอักษรละตินติดอยู่ — SHA/hex/รหัสรุ่น เป็นก้อนเดียว ไม่ใช่เลขที่อ่านได้
# (ตอนแรกเขียนไม่กัน ⇒ `f31e61b` ถูกแปลงเป็น `fสามสิบเอ็ดeหกสิบเอ็ดb` = พังกว่าเดิม)
# ปล่อยให้ **ด่านตีตกอย่างสะอาด** ดีกว่าพยายามอ่านให้ได้แบบผิดๆ แล้วส่งของครึ่งๆ ออกไป
_INT = re.compile(r"(?<![\d.A-Za-z])(\d+)(?![\d.A-Za-z])")
_DEC = re.compile(r"(?<![\dA-Za-z])(\d+)\.(\d+)(?![\dA-Za-z])")
_DIGIT_WORD = ["ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]


def _int_to_thai(n: str) -> str:
    """เลขจำนวนเต็ม -> คำอ่านไทย ด้วย pythainlp (ไม่เขียน converter เอง)

    ยาวเกินที่ lib รับได้ (เช่น hash หรือเลขพอร์ตติดกันยาวๆ) ให้อ่านทีละหลัก
    ดีกว่าโยน exception ทิ้งกลางทาง เพราะด่านข้างล่างจะจับให้อยู่แล้วถ้าอ่านไม่ได้

    pythainlp เป็น dependency ของ **local engine เท่านั้น** (requirements-local.txt)
    deploy แบบ Google-only ไม่ต้องลง — แต่ถ้าไม่ลงแล้วมาเรียก ต้องได้ error ที่บอกทางแก้
    ไม่ใช่ ModuleNotFoundError เปล่าๆ กลางทาง
    """
    try:
        from pythainlp.util import num_to_thaiword
    except ImportError as exc:      # pragma: no cover — ขึ้นเฉพาะตอนลง deps ไม่ครบ
        raise RuntimeError(
            "translit ต้องใช้ pythainlp — ลงด้วย `pip install -r requirements-local.txt` "
            "(จำเป็นเฉพาะเมื่อเปิด local engine)"
        ) from exc
    try:
        return num_to_thaiword(int(n))
    except (ValueError, OverflowError):
        return "".join(_DIGIT_WORD[int(d)] for d in n)


def translit(text: str) -> str:
    """แทนที่ยาวก่อนสั้น + ขอบเขตคำ แล้วคืนข้อความที่ไม่ควรมีอักษรละติน/เลขอารบิกเหลือ

    ลำดับสำคัญ: DICT (case-sensitive, ชื่อเฉพาะ) ก่อน DICT_CI (ไม่สนตัวพิมพ์)
    ไม่งั้น "Face" ใน "Hugging Face" จะถูกชุด CI กินไปก่อน
    """
    for src in sorted(DICT, key=len, reverse=True):
        # \b ใช้ไม่ได้ตรงๆ กับ '-' ใน co-founder จึงประกอบ boundary เอง
        pat = r"(?<![A-Za-z])" + re.escape(src) + r"(?![A-Za-z])"
        text = re.sub(pat, DICT[src], text)
    for src in sorted(DICT_CI, key=len, reverse=True):
        pat = r"(?<![A-Za-z])" + re.escape(src) + r"(?![A-Za-z])"
        text = re.sub(pat, DICT_CI[src], text, flags=re.IGNORECASE)
    for src in sorted(NUM, key=len, reverse=True):
        text = re.sub(r"(?<!\d)" + src + r"(?!\d)", NUM[src], text)
    # ทศนิยมก่อนจำนวนเต็ม — ไม่งั้น "2.5" จะกลายเป็น "สอง.ห้า"
    text = _DEC.sub(lambda m: f"{_int_to_thai(m.group(1))}จุด"
                              + "".join(_DIGIT_WORD[int(d)] for d in m.group(2)), text)
    text = _INT.sub(lambda m: _int_to_thai(m.group(1)), text)
    return text


def residual_latin(text: str):
    """ตัวที่ dict ยังไม่ครอบ — ต้องเป็นศูนย์ก่อนจะ synth (ด่านต้องหยุดได้จริง)"""
    return sorted(set(re.findall(r"[A-Za-z][A-Za-z\-]*", text)))


def residual_digits(text: str):
    return sorted(set(re.findall(r"\d+", text)))


# ---------------------------------------------------------------------------
# ชั้นหั่นข้อความ — ย้ายมาจาก AgentP-oracle/ψ/lab/chunk_th.py (อัลกอริทึมเดียวกันเป๊ะ)
#
# ⚠️ หนี้ที่รู้ตัวและยังไม่ปิด: ไฟล์นี้เป็นสำเนาที่สองของ ψ/lab/translit_th.py + chunk_th.py
# 3 ทางเลือก (แยก package / git submodule / ยอมรับสำเนาแล้วมีเทสต์เทียบ) ยังรอเคาะ
# เอามาไว้ที่นี่แทนที่จะสร้างไฟล์ใหม่ เพราะสำเนาที่สามแย่กว่าสำเนาที่สอง
#
# ทำไมต้องหั่นเอง ทั้งที่ TTS.infer() หั่นให้อยู่แล้ว — **วัดแล้วที่ 1/5/20 นาที**:
#   VRAM peak   lib 889 -> 1222 -> 2341 MB (โตตามความยาว)  ·  หั่นเอง 801 -> 804 -> 806 MB (คงที่)
#   เวลา        lib เร็วกว่า 22% (145.1 s vs 186.1 s ที่ ~20 นาที)
#   ความเงียบ   lib 0.9 s ตลอด 19 นาที · หั่นเอง 89.3 s ตลอด 20.4 นาที
#   ref drift   **ไม่มีทั้งสองแบบ** (|t| < 1.3 ตลอด 20 นาที)
# ⇒ เลือกหั่นเองเพราะ VRAM คงที่ ไม่ใช่เพราะเสียงดีกว่า — ข้อหลังยังไม่มีใครตัดสิน
#   (AgentP-oracle/ψ/memory/learnings/2026-07-30_longform-who-chunks.md)
#
# จุดตัดที่ยอมรับ เรียงตามความอยากได้:
#   1. ขอบย่อหน้า        -> เงียบ 0.55 s
#   2. ช่องว่างในย่อหน้า  -> เงียบ 0.22 s  (ภาษาไทยใช้ space แทนเครื่องหมายวรรคตอน)
#   3. ไม่มีช่องว่างเลย   -> ตัดดิบที่ MAX_CHUNK และ **นับไว้รายงาน ห้ามเงียบ**

MAX_CHUNK = 90
GAP_PARA = 0.55
GAP_MID = 0.22
LIB_MAX_CHARS = 200      # > MAX_CHUNK เพื่อให้ lib ไม่มีโอกาสหั่นก้อนของเราซ้ำ

_STRIP = str.maketrans({'"': None, "“": None, "”": None,
                        "‘": None, "’": None})


def paragraphs(text: str) -> list:
    return [ln.strip() for ln in text.translate(_STRIP).split("\n") if ln.strip()]


def split_para(p: str, max_chars: int = MAX_CHUNK) -> list:
    """คืน [(ก้อน, ตัดดิบไหม)] — ทุกก้อน <= max_chars และตัดที่ช่องว่างก่อนเสมอ"""
    out, cur = [], ""
    for tok in p.split(" "):
        if not tok:
            continue
        cand = tok if not cur else cur + " " + tok
        if len(cand) <= max_chars:
            cur = cand
            continue
        if cur:
            out.append((cur, False))
            cur = ""
        while len(tok) > max_chars:      # ก้อนเดียวยาวเกิน ไม่มีช่องว่าง = ตัดกลางคำ
            out.append((tok[:max_chars], True))
            tok = tok[max_chars:]
        cur = tok
    if cur:
        out.append((cur, False))
    return out


_SPEAKABLE = re.compile(r"[\u0E00-\u0E7Fa-zA-Z0-9]")


def is_speakable(chunk: str) -> bool:
    """มีอะไรให้อ่านออกเสียงไหม — เส้นคั่น '-----' หรือ '***' ไม่มี

    ทำไมต้องมี: บทความจริงของ Boommer มีเส้นคั่น Markdown อยู่กลางเรื่อง
    ก้อนนั้นถูกส่งเข้าโมเดลแล้วได้ **เสียงเปล่า** ⇒ concat ระเบิดด้วย error ของ numpy
    ที่อ่านไม่ออกว่าเกิดอะไร (array 0 มิติ) — เจอตอนยิงของจริง ไม่ใช่ตอนเทสต์
    """
    return bool(_SPEAKABLE.search(chunk))


def plan(text: str, max_chars: int = MAX_CHUNK):
    """คืน ([(chunk, gap_after_sec)], สถิติ) — gap คือความเงียบที่เราคุมเอง"""
    items, hard_cuts, dropped = [], 0, 0
    paras = paragraphs(text)
    if not paras:
        return [], {"paragraphs": 0, "chunks": 0, "hard_cuts": 0,
                    "max_chunk": 0, "mean_chunk": 0.0, "dropped_unspeakable": 0}
    for pi, p in enumerate(paras):
        chunks = split_para(p, max_chars)
        for ci, (c, hard) in enumerate(chunks):
            last = ci == len(chunks) - 1
            gap = GAP_PARA if last else GAP_MID
            if pi == len(paras) - 1 and last:
                gap = 0.0                # ไม่ต้องมีความเงียบต่อท้ายไฟล์
            if not is_speakable(c):
                dropped += 1          # นับไว้รายงาน ห้ามทิ้งเงียบ
                continue
            items.append((c, gap))
            hard_cuts += int(hard)
    if not items:
        return [], {"paragraphs": len(paras), "chunks": 0, "hard_cuts": 0,
                    "max_chunk": 0, "mean_chunk": 0.0, "dropped_unspeakable": dropped}
    return items, {"paragraphs": len(paras), "chunks": len(items), "hard_cuts": hard_cuts,
                   "max_chunk": max(len(c) for c, _ in items),
                   "mean_chunk": round(sum(len(c) for c, _ in items) / len(items), 1),
                   "dropped_unspeakable": dropped}

# ---- ชุดฟุตบอล/อาเซียน — เติมจากบทความจริงที่ Boommer ส่งมา 2026-07-30 ----
# ยาวก่อนสั้นถูกจัดการโดย translit() อยู่แล้ว (sorted by len) จึงใส่วลียาวปนได้
DICT.update({
    "FIFA Asean": "ฟีฟ่า อาเซียน",
    "Fifa Asean": "ฟีฟ่า อาเซียน",
    "Arab Cup": "อาหรับ คัพ",
    "Gulf Cup": "กัลฟ์ คัพ",
    "Head to Head": "เฮด ทู เฮด",
    "The best squad": "เดอะ เบสท์ สควอด",
    "Transfer Window": "ทรานสเฟอร์ วินโดว์",
    "AFF": "เอเอฟเอฟ",
    "FIFA": "ฟีฟ่า",
    "Fifa": "ฟีฟ่า",
    "Asean": "อาเซียน",
    "Final": "ไฟนอล",
    "Window": "วินโดว์",
    "Cup": "คัพ",
    "The": "เดอะ",
    "squad": "สควอด",
    "success": "ซัคเซส",
    "best": "เบสท์",
    "pain point": "เพน พอยต์",
    "pain": "เพน",
    "point": "พอยต์",
    "vs": "ปะทะ",
    # ชุด A/B/C = ทีมชุดหนึ่ง/สอง/สาม — ตัวอักษรเดี่ยวต้องมี boundary ซึ่ง translit() ใส่ให้แล้ว
    "A": "เอ",
    "B": "บี",
    "C": "ซี",
    # "x5" = สัมประสิทธิ์คูณห้า — ตัว x ติดเลขจึงต้องแทนก่อนขั้นแปลงตัวเลข
    "x": "คูณ",
})
