"""Rock 引擎内存仲裁的单测。

覆盖 runtime.py 的三个关键行为:
  1. 内存够 -> 两个槽位可以同时驻留(zram 兜底场景)
  2. 内存不够 -> 大模型让位给小模型(纯 RAM 场景)
  3. 空闲到期 -> 内存还宽裕就留着(不白折腾), 紧张就卸载
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "board-server" / "pkg"))

from core.providers.rock import runtime  # noqa: E402


class FakeHelper:
    """假的 HelperProcess: 只记录 start/stop。"""

    def __init__(self, *a, **kw):
        self.started = 0
        self.stopped = 0
        self._alive = False

    @property
    def alive(self):
        return self._alive

    def start(self):
        self.started += 1
        self._alive = True

    def stop(self):
        self.stopped += 1
        self._alive = False


class SlotTests(unittest.TestCase):
    def setUp(self):
        runtime.reset_slots()
        self.helpers = []

        def factory():
            h = FakeHelper()
            self.helpers.append(h)
            return h

        self.factory = factory

    def tearDown(self):
        runtime.reset_slots()

    def test_acquire_starts_helper_once(self):
        slot = runtime.get_slot("asr", self.factory, footprint_mb=600,
                                idle_timeout_s=0)
        h1 = slot.acquire()
        h2 = slot.acquire()
        self.assertIs(h1, h2)
        self.assertEqual(len(self.helpers), 1)
        self.assertEqual(h1.started, 1)

    def test_memory_tight_makes_big_model_yield(self):
        """内存不够时, 大模型先让位 —— 这就是 2GB 板子的保命逻辑。"""
        asr = runtime.get_slot("asr", self.factory, footprint_mb=600,
                               idle_timeout_s=0)
        tts = runtime.get_slot("tts", self.factory, footprint_mb=250,
                               idle_timeout_s=0)
        # 只有 300MB 可用: 装不下 ASR(600), 只装得下 TTS(250)
        with patch.object(runtime, "mem_available_mb", return_value=300):
            asr.acquire()
            tts.acquire()
        # ASR 被 TTS 挤掉, TTS 活着
        self.assertEqual(len(self.helpers), 2)
        self.assertFalse(self.helpers[0].alive)
        self.assertTrue(self.helpers[1].alive)

    def test_memory_ample_keeps_both(self):
        """内存宽裕(zram 兜底)时两个模型可以同时驻留。"""
        asr = runtime.get_slot("asr", self.factory, footprint_mb=600,
                               idle_timeout_s=0)
        tts = runtime.get_slot("tts", self.factory, footprint_mb=250,
                               idle_timeout_s=0)
        with patch.object(runtime, "mem_available_mb", return_value=2000):
            asr.acquire()
            tts.acquire()
        self.assertTrue(all(h.alive for h in self.helpers))

    def test_idle_stop_keeps_helper_when_memory_ample(self):
        """空闲到期但内存宽裕 -> 留着, 省下次的加载延迟。"""
        slot = runtime.get_slot("asr", self.factory, footprint_mb=600,
                                idle_timeout_s=0)
        with patch.object(runtime, "mem_available_mb", return_value=2000):
            h = slot.acquire()
            slot._idle_stop()
        self.assertTrue(h.alive)
        self.assertEqual(h.stopped, 0)

    def test_idle_stop_unloads_when_memory_tight(self):
        """空闲到期且内存紧张 -> 卸载, 把 NPU 内存还给系统。"""
        slot = runtime.get_slot("asr", self.factory, footprint_mb=600,
                                idle_timeout_s=0)
        with patch.object(runtime, "mem_available_mb", return_value=100):
            h = slot.acquire()
            slot._idle_stop()
        self.assertFalse(h.alive)
        self.assertEqual(h.stopped, 1)

    def test_stop_all_releases_everything(self):
        asr = runtime.get_slot("asr", self.factory, footprint_mb=600,
                               idle_timeout_s=0)
        tts = runtime.get_slot("tts", self.factory, footprint_mb=250,
                               idle_timeout_s=0)
        with patch.object(runtime, "mem_available_mb", return_value=5000):
            asr.acquire()
            tts.acquire()
        runtime.stop_all()
        self.assertTrue(all(not h.alive for h in self.helpers))

    def test_mem_available_includes_zram_headroom(self):
        """有 zram 时可用内存要算上它还能吃下的量。"""
        raw = 200
        zram_free = 500
        with patch.object(runtime, "_mem_available_raw_mb", return_value=raw), \
             patch.object(runtime, "_zram_free_mb", return_value=zram_free):
            got = runtime.mem_available_mb()
        self.assertGreater(got, raw)
        self.assertLessEqual(got, raw + zram_free * runtime.COMPRESS_RATIO + 1)


if __name__ == "__main__":
    unittest.main()
