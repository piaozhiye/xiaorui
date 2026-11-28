#!/usr/bin/env python3
"""Security checks for the Music Assistant kiosk launcher."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ui" / "ma-kiosk.sh"


class MaKioskTests(unittest.TestCase):
    def run_kiosk(self, url):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chromium = root / "chromium"
            chromium.write_text("#!/bin/sh\nexit 0\n")
            chromium.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "XR_MA_URL": url,
                "XR_MA_KIOSK_DATA_DIR": str(root / "data"),
                "XR_CHROMIUM_BIN": str(chromium),
            })
            result = subprocess.run(["sh", str(SCRIPT)], env=env,
                                    text=True, capture_output=True, timeout=5)
            manifest_path = root / "data/return-extension/manifest.json"
            manifest = manifest_path.read_text() if manifest_path.exists() else None
            return result, manifest

    def test_extension_permissions_match_only_the_configured_ma_origin(self):
        result, raw_manifest = self.run_kiosk("https://music.example:9443/ui")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(raw_manifest)
        manifest = json.loads(raw_manifest)
        expected = ["https://music.example:9443/*"]
        self.assertEqual(manifest["host_permissions"], expected)
        self.assertEqual(manifest["content_scripts"][0]["matches"], expected)

    def test_url_userinfo_is_rejected_without_exposing_credentials(self):
        result, _ = self.run_kiosk("https://user:private-secret@music.example:9443")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("private-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
