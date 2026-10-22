import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from soak_health import HealthGuard, cleanup_confirmed


class HealthTests(unittest.TestCase):
    def test_cleanup(self):
        for reply in ("当前没有正在进行的提醒", "好的，已取消所有提醒"):
            self.assertTrue(cleanup_confirmed(reply))
        for reply in (None, "", "是否取消提醒？", "好的，三分钟后提醒你"):
            self.assertFalse(cleanup_confirmed(reply))

    def test_dma_budget_does_not_hide_socket_leak(self):
        guard = HealthGuard()
        guard.check("front", "boot:123:456", 1000, 100, dma=80, now=0)
        guard.check("front", "boot:123:456", 1000, 180, dma=160, now=30)
        with self.assertRaises(RuntimeError):
            guard.check("front", "boot:123:456", 1000, 189, dma=160, now=60)

    def test_dma_must_plateau(self):
        guard = HealthGuard()
        guard.check("front", "id", 1000, 100, dma=80, now=0)
        guard.check("front", "id", 1000, 140, dma=120, now=120)
        guard.check("front", "id", 1000, 148, dma=128, now=150)
        with self.assertRaises(RuntimeError):
            guard.check("front", "id", 1000, 149, dma=129, now=180)

    def test_progress(self):
        HealthGuard.check_progress(10000, 20000)
        for tick in (0, 20001, 1):
            with self.assertRaises(RuntimeError):
                HealthGuard.check_progress(tick, 200000 if tick == 1 else 20000)

    def test_stable(self):
        guard = HealthGuard()
        guard.check("front", "boot:123:456", 1000, 186)
        guard.check("front", "boot:123:456", 1100, 187)

    def test_fail_closed(self):
        for identity, rss, fd in [("boot:124:456", 1000, 186),
                                  ("newboot:123:456", 1000, 186),
                                  ("boot:123:999", 1000, 186),
                                  ("boot:123:456", 1000, 286),
                                  ("boot:123:456", 140000, 186),
                                  ("", 0, 0)]:
            with self.subTest(identity=identity, rss=rss, fd=fd):
                guard = HealthGuard()
                guard.check("front", "boot:123:456", 1000, 186)
                with self.assertRaises(RuntimeError):
                    guard.check("front", identity, rss, fd)


if __name__ == "__main__":
    unittest.main()
