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
        with patch.object(runtime, "mem_available_raw_mb", return_value=2000):
            asr.acquire()
        # TTS 需要 650MB；停止 ASR 后空闲预算增至 1000MB。
        with patch.object(runtime, "mem_available_raw_mb",
                          side_effect=lambda: 100 if self.helpers[0].alive else 1000):
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
        with patch.object(runtime, "mem_available_raw_mb", return_value=2000):
            asr.acquire()
            tts.acquire()
        self.assertTrue(all(h.alive for h in self.helpers))

    def test_idle_stop_keeps_helper_when_memory_ample(self):
        """空闲到期但内存宽裕 -> 留着, 省下次的加载延迟。"""
        slot = runtime.get_slot("asr", self.factory, footprint_mb=600,
                                idle_timeout_s=0)
        with patch.object(runtime, "mem_available_raw_mb", return_value=2000):
            h = slot.acquire()
            slot._idle_stop()
        self.assertTrue(h.alive)
        self.assertEqual(h.stopped, 0)

    def test_idle_stop_unloads_when_memory_tight(self):
        """空闲到期且内存紧张 -> 卸载, 把 NPU 内存还给系统。"""
        slot = runtime.get_slot("asr", self.factory, footprint_mb=600,
                                idle_timeout_s=0)
        with patch.object(runtime, "mem_available_raw_mb", return_value=2000):
            h = slot.acquire()
        with patch.object(runtime, "mem_available_raw_mb", return_value=100):
            slot._idle_stop()
        self.assertFalse(h.alive)
        self.assertEqual(h.stopped, 1)

    def test_stop_all_releases_everything(self):
        asr = runtime.get_slot("asr", self.factory, footprint_mb=600,
                               idle_timeout_s=0)
        tts = runtime.get_slot("tts", self.factory, footprint_mb=250,
                               idle_timeout_s=0)
        with patch.object(runtime, "mem_available_raw_mb", return_value=5000):
            asr.acquire()
            tts.acquire()
        runtime.stop_all()
        self.assertTrue(all(not h.alive for h in self.helpers))

    def test_mem_available_counts_only_net_cold_anon_reclaim(self):
        with patch.object(runtime, "_meminfo", return_value={
                "MemAvailable": 200, "Inactive(anon)": 400}), \
             patch.object(runtime, "_zram_free_mb", return_value=500):
            self.assertEqual(runtime.mem_available_mb(),
                             200 + int(400 * (1 - 1/runtime.COMPRESS_RATIO)))

    def test_no_cold_anon_means_no_extra_npu_budget(self):
        with patch.object(runtime, "_meminfo", return_value={"MemAvailable": 100}), \
             patch.object(runtime, "_zram_free_mb", return_value=768):
            self.assertEqual(runtime.mem_available_mb(), 100)

    def test_insufficient_memory_does_not_start_helper(self):
        slot = runtime.get_slot("asr", self.factory, 600, 0)
        with patch.object(runtime, "mem_available_raw_mb", return_value=0):
            with self.assertRaises(runtime.HelperError):
                slot.acquire()
        self.assertEqual(self.helpers, [])

    def test_zram_uses_active_swap_logical_free_capacity(self):
        from io import StringIO
        content = "Filename Type Size Used Priority\n/dev/zram0 partition 786432 786432 5\n"
        with patch("builtins.open", side_effect=lambda *a: StringIO(content)):
            self.assertEqual(runtime._zram_free_mb(), 0)
        content = "Filename Type Size Used Priority\n/dev/zram0 partition 786432 262144 5\n"
        with patch("builtins.open", side_effect=lambda *a: StringIO(content)):
            self.assertEqual(runtime._zram_free_mb(), 512)
        content = "Filename Type Size Used Priority\n"
        with patch("builtins.open", side_effect=lambda *a: StringIO(content)):
            self.assertEqual(runtime._zram_free_mb(), 0)


if __name__ == "__main__":
    unittest.main()
