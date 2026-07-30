"""mutation-replay ของด่าน relay — พิสูจน์ว่าเทสต์แต่ละใบ *กัน* อะไรอยู่จริง

วิธี: แก้ `relay_auth.py` ทีละจุด (mutant) → รันเทสต์ → บันทึกว่าใบไหนแดง → คืนไฟล์เดิม
เกณฑ์ผ่านของแต่ละ mutant = **เทสต์ที่ตั้งใจให้จับ แดงจริง** ไม่ใช่แค่ "มีอะไรแดงก็พอ"

mutant ที่ "SURVIVED" (ไม่มีเทสต์แดง) = ช่องที่เทสต์ชุดนี้ยังไม่ครอบ — ต้องรายงาน ไม่ใช่ซ่อน
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "relay_auth.py"
PY = sys.executable

MUTANTS = [
    ("sig-never-rejects",
     "if not hmac.compare_digest(expect, sig):",
     "if False:  # MUTANT",
     ["MutationReplay", "PositiveControl.test_decision_is_truthy"]),

    ("sig-uses-plain-equals",
     "if not hmac.compare_digest(expect, sig):",
     "if expect != sig:  # MUTANT",
     ["TimingSafeCompareIsSourceLevelOnly"]),   # เดิม SURVIVED — ปิดด้วยด่าน source — เทสต์ functional จับ timing attack ไม่ได้ ต้องยอมรับตรงๆ

    ("body-not-covered-by-signature",
     'return "\\n".join([ts, nonce, chat_id, hashlib.sha256(body).hexdigest()])',
     'return "\\n".join([ts, nonce, chat_id])  # MUTANT',
     ["MutationReplay.test_body_tampered_by_one_byte"]),

    ("chat-id-not-covered-by-signature",
     'return "\\n".join([ts, nonce, chat_id, hashlib.sha256(body).hexdigest()])',
     'return "\\n".join([ts, nonce, hashlib.sha256(body).hexdigest()])  # MUTANT',
     ["MutationReplay.test_chat_id_swapped_to_another_allowed_chat"]),

    ("allowlist-empty-means-allow-all (ยืม semantics ของ bot.py มาผิดที่)",
     "if chat_id not in allowed_chat_ids:",
     "if allowed_chat_ids and chat_id not in allowed_chat_ids:  # MUTANT",
     ["Allowlist.test_empty_allowlist_denies_everyone",
      "Allowlist.test_relay_default_is_deliberately_stricter_than_bot"]),

    ("future-timestamp-allowed",
     "if abs(now - ts) > MAX_SKEW_SECONDS:",
     "if now - ts > MAX_SKEW_SECONDS:  # MUTANT",
     ["TimestampWindow.test_too_far_in_future_rejected"]),

    ("nonce-checked-before-signature (เผา nonce ได้โดยไม่มีกุญแจ)",
     "    expect = expected_signature(secret, ts_raw, nonce, chat_raw, body)",
     "    nonce_cache.purge(now)\n"
     "    if nonce_cache.seen(nonce):\n"
     "        return Decision(False, 'replayed_nonce', 409)\n"
     "    nonce_cache.remember(nonce, ts)  # MUTANT\n"
     "    expect = expected_signature(secret, ts_raw, nonce, chat_raw, body)",
     ["NonceReplay.test_rejected_request_does_not_burn_the_nonce"]),

    ("full-cache-flushes-instead-of-refusing",
     "    if nonce_cache.is_full():",
     "    if False:  # MUTANT",
     ["NonceReplay.test_full_cache_rejects_instead_of_flushing"]),

    ("blank-secret-opens-endpoint",
     'clean_secret = (secret or "").strip()',
     'clean_secret = (secret or "")  # MUTANT',
     ["FailClosedWithoutSecret.test_whitespace_secret_is_not_a_secret"]),

    ("no-secret-check-removed-entirely",
     "    if not clean_secret:",
     "    if False:  # MUTANT",
     ["FailClosedWithoutSecret"]),
]


def run_tests():
    p = subprocess.run(
        [PY, "-m", "pytest", "tests/test_relay_auth.py", "-q", "--no-header", "-rf"],
        cwd=REPO, capture_output=True, text=True,
    )
    failed = set()
    for line in (p.stdout + p.stderr).splitlines():
        line = line.strip()
        if line.startswith(("FAILED", "SUBFAILED", "ERROR")):
            # รูป: FAILED tests/test_relay_auth.py::Class::test_name
            part = line.split("::", 1)
            if len(part) == 2:
                failed.add(part[1].split(" ")[0])
    return p.returncode, failed


def main():
    src = TARGET.read_text(encoding="utf-8")
    backup = TARGET.with_suffix(".py.mutbak")
    shutil.copy2(TARGET, backup)
    results = []
    try:
        rc, failed = run_tests()
        if rc != 0:
            print(json.dumps({"error": "baseline ไม่เขียว — หยุด", "failed": sorted(failed)},
                             ensure_ascii=False, indent=2))
            return 1
        print("baseline: เขียว ✅ (positive control ผ่าน — เทสต์ปฏิเสธจึงมีความหมาย)\n")

        for name, old, new, expect_hits in MUTANTS:
            if old not in src:
                results.append({"mutant": name, "verdict": "PATCH-MISS",
                                "note": "หา pattern ไม่เจอ — สคริปต์เองผิด ไม่ใช่ code ผิด"})
                print(f"  ⚠️  {name}: PATCH-MISS")
                continue
            TARGET.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, failed = run_tests()
            hit = sorted(failed)
            if rc == 0:
                verdict = "SURVIVED"
            else:
                # 🔴 รอบแรกเขียน `e in f` โดย e ใช้ "." แต่ pytest คืน "::" ⇒ ไม่เคย match
                # ⇒ ทุก mutant ขึ้น KILLED-PARTIAL · **ตัวตรวจ "แดงด้วยเหตุผลที่ตั้งใจ"
                # ดันเป็นตัวที่แดงด้วยเหตุผลที่ไม่ได้ตั้งใจเอง** — ตระกูล B เป๊ะ
                norm = [e.replace(".", "::") for e in expect_hits]
                matched = [e for e in norm if any(e in f for f in failed)]
                verdict = "KILLED-AS-INTENDED" if (expect_hits and len(matched) == len(expect_hits)) \
                    else ("KILLED-BY-OTHER" if not expect_hits else "KILLED-PARTIAL")
            results.append({"mutant": name, "verdict": verdict,
                            "expected_to_catch": expect_hits, "actually_red": hit})
            mark = {"KILLED-AS-INTENDED": "✅", "SURVIVED": "🔴",
                    "KILLED-PARTIAL": "🟡", "KILLED-BY-OTHER": "🟡"}[verdict]
            print(f"  {mark} {name}: {verdict}  (แดง {len(hit)} ใบ)")
            TARGET.write_text(src, encoding="utf-8")
    finally:
        shutil.copy2(backup, TARGET)
        backup.unlink()

    rc, failed = run_tests()
    print(f"\nคืนไฟล์แล้ว — รันซ้ำ: {'เขียว ✅' if rc == 0 else 'แดง 🔴 ' + str(sorted(failed))}")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/mutation-replay.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"รายงาน: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
