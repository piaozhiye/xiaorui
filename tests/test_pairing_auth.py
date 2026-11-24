import hashlib
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "snapcast_center"))

from pairing_auth import PairingAuth  # noqa: E402


class PairingAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.now = [1000]
        self.state = Path(self.tempdir.name) / "state.json"
        self.code_path = Path(self.tempdir.name) / "code.json"
        self.auth = PairingAuth(self.state, self.code_path, clock=lambda: self.now[0])

    def test_pair_code_is_one_time_and_persists_only_token_hash(self):
        code = self.auth.code
        result = self.auth.pair(code, "  Phone\nControl  ", "192.168.1.20")
        self.assertIsNotNone(result)
        self.assertTrue(self.auth.authorize(result["token"]))
        self.assertIsNone(self.auth.code)
        self.assertIsNone(self.auth.pair(code, "second", "192.168.1.21"))
        payload = json.loads(self.state.read_text())
        saved = payload["devices"][result["device_id"]]
        self.assertEqual(saved["token_hash"], hashlib.sha256(result["token"].encode()).hexdigest())
        self.assertNotIn(result["token"], self.state.read_text())
        self.assertEqual(saved["name"], "PhoneControl")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_code_expires_and_wrong_attempts_are_limited_per_address(self):
        code = self.auth.code
        self.now[0] += PairingAuth.CODE_TTL_SECONDS + 1
        self.assertIsNone(self.auth.code)
        for _ in range(PairingAuth.MAX_ATTEMPTS_PER_ADDRESS):
            self.assertIsNone(self.auth.pair(code, "phone", "192.168.1.20"))
        self.assertIsNone(self.auth.pair(code, "phone", "192.168.1.20"))
        new_code = self.auth.rotate_code()
        self.assertNotEqual(code, new_code)
        self.assertIsNotNone(self.auth.pair(new_code, "phone", "192.168.1.20"))

    def test_successful_code_can_only_be_consumed_once_under_concurrency(self):
        code = self.auth.code
        barrier = threading.Barrier(8)

        def attempt(index):
            barrier.wait()
            return self.auth.pair(code, f"phone-{index}", f"192.168.1.{index + 1}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.auth.devices()), 1)

    def test_failed_attempts_are_capped_across_addresses_until_rotation(self):
        code = self.auth.code
        for index in range(PairingAuth.MAX_ATTEMPTS_PER_CODE):
            self.assertIsNone(self.auth.pair("wrong", "phone", f"192.168.1.{index + 1}"))
        self.assertIsNone(self.auth.pair(code, "phone", "192.168.2.1"))
        new_code = self.auth.rotate_code()
        self.assertIsNotNone(self.auth.pair(new_code, "phone", "192.168.2.1"))

    def test_credentials_survive_restart_and_can_be_revoked(self):
        result = self.auth.pair(self.auth.code, "phone", "192.168.1.20")
        restarted = PairingAuth(self.state, self.code_path, clock=lambda: self.now[0])
        self.assertTrue(restarted.authorize(result["token"]))
        self.assertTrue(restarted.revoke(result["device_id"]))
        self.assertFalse(restarted.authorize(result["token"]))
        self.assertFalse(restarted.revoke(result["device_id"]))


if __name__ == "__main__":
    unittest.main()
