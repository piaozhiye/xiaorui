import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "live_smoke", Path(__file__).resolve().parents[1] / "tools/run-live-smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeTests(unittest.TestCase):
    def test_unicode_log(self):
        with patch.object(smoke, "offset", return_value=100), patch.object(
                smoke, "board", return_value=r"[xr] ASR 回执: \u73b0\u5728几点了"):
            self.assertEqual(smoke.segment(0), "[xr] ASR 回执: 现在几点了")

    def test_log_truncation(self):
        with patch.object(smoke, "offset", return_value=10):
            with self.assertRaises(RuntimeError):
                smoke.segment(20)


if __name__ == "__main__":
    unittest.main()
