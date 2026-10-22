"""Failure-path regression without touching a board or playing audio."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LifecycleTests(unittest.TestCase):
    def test_ready_requires_calibrated_live_capture(self):
        source = (ROOT / "board-server/start-vqe-accept.sh").read_text()
        start = source.index("candidate_ready() {")
        function = source[start:source.index("\n}", start) + 2]
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "capture.log"
            for text, ready in (("[xr] mic=xr_vqe_16k 聆听中\n", False),
                                ("[xr] mic=xr_vqe_16k 聆听中\n[xr] ambient=-52 gate=-46\n", True)):
                log.write_text(text)
                result = subprocess.run(["sh", "-c", function +
                                         '\nLOG="$1"; CAND_PID=$$; candidate_ready',
                                         "test", str(log)])
                self.assertEqual(result.returncode == 0, ready)

    def run_case(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            commands = base / "commands"
            audio = base / "audio"
            audio.mkdir()
            for name in ("wake2_time", "wake2_weather", "wake2_stop", "wake2_story",
                         "wake2_name", "long_cook", "long_history", "cmd_joke"):
                (audio / (name + ".wav")).write_bytes(b"mock fixture")
            fake = base / "adb"
            fake.write_text('''#!/usr/bin/env python3
import os, sys
with open(os.environ["COMMANDS"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
command = sys.argv[-1]
if "sha256sum " in command:
    print(os.environ["PLUGIN_SHA"] + "  plugin")
elif "sh /data/xiaorui-gateway/start-vqe-accept.sh" in command:
    print("READY" if os.environ["MODE"] == "wrong-plugin" else "NOT-READY")
    sys.exit(1 if os.environ["MODE"] == "failed-launch" else 0)
elif "/maps" in command:
    print("/usr/lib/alsa-lib/libasound_module_pcm_rkaudio_vqe.so")
elif "cat /tmp/vqe_cand.pid" == command:
    print("1234")
''')
            fake.chmod(0o755)
            for name in ("sleep", "aplay"):
                script = base / name
                script.write_text("#!/bin/sh\nexit 0\n")
                script.chmod(0o755)
            digest = hashlib.sha256((ROOT / "vendor/rkaudio-vqe/libasound_module_pcm_rkaudio_vqe.so").read_bytes()).hexdigest()
            result = subprocess.run(
                ["bash", str(ROOT / "tools/test-es8388-vqe-chain.sh"), str(base / "out")],
                env=dict(os.environ, PATH=str(base) + ":" + os.environ["PATH"],
                         COMMANDS=str(commands), MODE=mode, PLUGIN_SHA=digest,
                         XZ_BOARD_SERIAL="test-vqe-lifecycle", XR_VQE_AUDIO_DIR=str(audio)),
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            calls = commands.read_text()
            self.assertIn("kill -TERM", calls)
            self.assertIn("/data/xiaorui-server/board-scripts/xiaorui_live", calls)
            self.assertNotIn("tail -c", calls)
            self.assertNotIn("RESULT 5/5", result.stdout)

    def test_not_ready_is_not_ready(self):
        self.run_case("not-ready")

    def test_failed_start_restores_production(self):
        self.run_case("failed-launch")

    def test_system_plugin_cannot_impersonate_candidate(self):
        self.run_case("wrong-plugin")


if __name__ == "__main__":
    unittest.main()
