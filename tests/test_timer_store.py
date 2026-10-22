import concurrent.futures
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server/pkg"))
from core.timer_store import TimerStore


class TimerStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state/timers.sqlite3"
        self.store = TimerStore(self.path)

    def test_restart_and_device_isolation(self):
        self.store.add("a", {"seconds": 30, "speak": "三十秒"}, 100)
        recovered = TimerStore(self.path)
        self.assertEqual(recovered.list("a")[0]["due_at"], 130)
        self.assertFalse(recovered.list("b"))
        self.assertFalse(recovered.cancel("b", {"op": "cancel"}))
        self.assertEqual(len(recovered.list("a")), 1)

    def test_concurrent_gateways_claim_only_one(self):
        self.store.add("a", {"seconds": 10, "speak": "十秒"}, 100)
        other = TimerStore(self.path)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda pair: pair[0].claim("a", pair[1], 111),
                                    [(self.store, "cloud"), (other, "local")]))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_lease_expiry_and_stale_owner(self):
        ident = self.store.add("a", {"seconds": 10, "speak": "十秒"}, 100)
        self.assertIsNotNone(self.store.claim("a", "dead", 111, lease_s=1))
        self.assertIsNone(self.store.claim("a", "live", 111))
        self.assertIsNotNone(self.store.claim("a", "live", 113))
        self.store.finish(ident, "dead", True, 114)
        self.assertTrue(self.store.owned(ident, "live"))
        self.store.finish(ident, "live", True, 114)
        self.assertFalse(self.store.list("a"))

    def test_cancel_claimed_cannot_resurrect(self):
        ident = self.store.add("a", {"seconds": 10, "speak": "十秒"}, 100)
        self.store.claim("a", "cloud", 111)
        self.store.cancel("a", {"op": "cancel", "seconds": 10})
        self.assertFalse(self.store.owned(ident, "cloud"))
        self.store.finish(ident, "cloud", False, 112)
        self.assertFalse(self.store.list("a"))

    def test_cross_date_and_duration_cancel(self):
        for target in (1000, 87400):
            self.store.add("a", {"seconds": target, "target_at": target,
                               "h24": 8, "minute": 0, "speak": "八点"}, 0)
        self.store.add("a", {"seconds": 30, "speak": "三十秒"}, 0)
        self.store.cancel("a", {"op": "cancel_clock", "target_at": 1000})
        self.assertEqual(len(self.store.list("a")), 2)
        self.store.cancel("a", {"op": "invalid_cancel"})
        self.assertEqual(len(self.store.list("a")), 2)
        self.store.cancel("a", {"op": "cancel", "seconds": 30})
        self.assertEqual(self.store.list("a")[0]["target_at"], 87400)

    def test_inactive_gateway_yields_to_other_owner(self):
        ident = self.store.add("a", {"seconds": 10, "speak": "十秒"}, 100)
        self.store.claim("a", "inactive", 111)
        self.store.finish(ident, "inactive", False, 111)
        self.assertIsNone(self.store.claim("a", "inactive", 112))
        self.assertIsNotNone(self.store.claim("a", "active", 112))

    def test_cap_and_expiry(self):
        for _ in range(3):
            self.assertIsNotNone(self.store.add("a", {"seconds": 10, "speak": "十秒"}, 0))
        self.assertIsNone(self.store.add("a", {"seconds": 10, "speak": "十秒"}, 0))
        self.store.expire(4000)
        self.assertFalse(self.store.list("a"))

    def test_dead_device_rows_expired_by_any_sweep(self):
        """2026-10-22 实测反例: 短命设备(测试探针)设的计时器, 该设备再无
        活服务——任何活设备的清扫必须把它过期, 否则永久 pending。"""
        self.store.add("ghost", {"seconds": 30, "speak": "三十秒"}, 100)   # due=130, 严重迟到
        self.store.add("a", {"seconds": 30, "speak": "三十秒"}, 3900)     # due=3930, 新鲜
        self.store.expire(4000)  # 设备 a 的活服务在跑清扫
        self.assertFalse(self.store.list("ghost"))
        self.assertEqual(len(self.store.list("a")), 1)


if __name__ == "__main__":
    unittest.main()
