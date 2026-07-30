import asyncio
import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

import bot


def _job(chat_id: int, tag: str) -> bot.TTSJob:
    return bot.TTSJob(
        chat_id=chat_id,
        status_message_id=1,
        text=tag,
        parts=[tag],
        queued_at=datetime.now(ZoneInfo("Asia/Bangkok")),
    )


class FairJobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_chat_keeps_fifo_order(self):
        """chat เดียว = พฤติกรรมเดิมเป๊ะ — การเปลี่ยนคิวต้องไม่เปลี่ยนอะไรสำหรับผู้ใช้คนเดียว"""
        queue = bot.FairJobQueue()
        for tag in ["a", "b", "c"]:
            await queue.put(_job(1, tag))

        got = [(await queue.get()).text for _ in range(3)]

        self.assertEqual(got, ["a", "b", "c"])
        self.assertEqual(queue.qsize(), 0)

    async def test_long_job_from_one_chat_does_not_starve_another(self):
        """หัวใจของ PR นี้ — และเป็นเทสที่ asyncio.Queue เดิม **ต้องไม่ผ่าน**

        chat 1 ส่งงานยาว 5 ชิ้นก่อน แล้ว chat 2 ส่ง 1 ชิ้นทีหลัง
        คิวเดิม (FIFO): chat 2 ได้คิวที่ 6
        คิวใหม่ (round-robin): chat 2 ได้คิวที่ 2
        """
        queue = bot.FairJobQueue()
        for i in range(5):
            await queue.put(_job(1, f"long-{i}"))
        await queue.put(_job(2, "short"))

        served = [(await queue.get()) for _ in range(6)]
        order = [(job.chat_id, job.text) for job in served]

        self.assertEqual(order[0], (1, "long-0"))
        self.assertEqual(order[1], (2, "short"))          # ← ไม่ต้องรอ chat 1 จบ
        self.assertEqual([chat for chat, _ in order[2:]], [1, 1, 1, 1])

    async def test_three_chats_rotate_evenly(self):
        queue = bot.FairJobQueue()
        for chat_id in (1, 2, 3):
            for i in range(2):
                await queue.put(_job(chat_id, f"{chat_id}-{i}"))

        order = [(await queue.get()).chat_id for _ in range(6)]

        self.assertEqual(order, [1, 2, 3, 1, 2, 3])

    async def test_get_waits_until_a_job_arrives(self):
        queue = bot.FairJobQueue()
        getter = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        self.assertFalse(getter.done())                   # ว่าง = ต้องรอ ไม่ใช่คืน None

        await queue.put(_job(7, "late"))
        job = await asyncio.wait_for(getter, timeout=1)

        self.assertEqual((job.chat_id, job.text), (7, "late"))

    async def test_chat_that_emptied_can_rejoin_without_jumping_ahead(self):
        queue = bot.FairJobQueue()
        await queue.put(_job(1, "a1"))
        await queue.put(_job(2, "b1"))
        await queue.put(_job(2, "b2"))

        self.assertEqual((await queue.get()).chat_id, 1)   # chat 1 หมดคิวย่อย
        await queue.put(_job(1, "a2"))                     # กลับเข้ามาใหม่

        rest = [(await queue.get()).text for _ in range(3)]

        self.assertEqual(rest, ["b1", "a2", "b2"])         # ต่อท้าย ไม่แซง b2

    async def test_counters_track_total_and_per_chat(self):
        queue = bot.FairJobQueue()
        await queue.put(_job(1, "a"))
        await queue.put(_job(1, "b"))
        await queue.put(_job(2, "c"))

        self.assertEqual(queue.qsize(), 3)
        self.assertEqual(queue.pending_for(1), 2)
        self.assertEqual(queue.pending_for(2), 1)
        self.assertEqual(queue.pending_for(999), 0)

        await queue.get()

        self.assertEqual(queue.qsize(), 2)
        self.assertEqual(queue.pending_for(1), 1)

    async def test_worker_interface_still_matches_old_queue(self):
        """worker เดิมเรียก get / task_done — ต้องเรียกได้เหมือนเดิม ไม่ต้องแก้ _tts_worker"""
        queue = bot.FairJobQueue()
        await queue.put(_job(1, "a"))

        job = await queue.get()
        queue.task_done()

        self.assertEqual(job.text, "a")

    def test_get_queue_returns_fair_queue_singleton(self):
        bot.JOB_QUEUE = None
        try:
            first = bot._get_queue()
            self.assertIsInstance(first, bot.FairJobQueue)
            self.assertIs(bot._get_queue(), first)
        finally:
            bot.JOB_QUEUE = None


if __name__ == "__main__":
    unittest.main()
