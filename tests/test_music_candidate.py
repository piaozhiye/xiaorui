"""Offline checks for candidate harness; never connects to ADB or audio devices."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "tools/test-music-candidate.py"


class CleanupTests(unittest.TestCase):
    def run_failure(self, failure, resume_works=True):
        spec = importlib.util.spec_from_file_location("music_candidate", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        baseline = dict(health={"8001": {"ready": True}, "8002": {"ready": True}},
                        env_hash={"8001": "one", "8002": "two"})
        state = dict(paused=False, resumes=0, frontend_stops=0)
        digest = hashlib.sha256(b"candidate fixture").hexdigest()

        def run(args, **kwargs):
            output = ""
            if args[0] == "adb" and "shell" in args:
                cmd = args[-1]
                script = kwargs.get("input", "")
                if cmd == module.PYTHON + " -":
                    if "found=[]" in script:
                        output = json.dumps([123] if "target=" + repr(module.PROD) in script else [])
                    elif "urllib.request" in script:
                        output = json.dumps(baseline)
                    elif "XR_CODE_DIR=(.*)" in script:
                        output = module.BASE + "/releases/prod/server"
                elif cmd == "cat /proc/device-tree/compatible": output = "rockchip,rk3576"
                elif cmd.startswith("sha256sum "): output = digest + "  fixture"
                elif cmd == "pidof crond": output = "200"
                elif cmd.startswith("awk '/^State:"): output = "T" if state["paused"] else "S"
                elif cmd == "kill -STOP 200":
                    state["paused"] = True
                    if failure == "pause-ack-lost":
                        raise subprocess.TimeoutExpired(args, 1)
                elif cmd == "kill -CONT 200":
                    state["resumes"] += 1
                    if resume_works: state["paused"] = False
                elif cmd == "kill -TERM 123": state["frontend_stops"] += 1
                elif cmd.startswith("mkdir -p ") and cmd.endswith("/uttdump"):
                    raise subprocess.CalledProcessError(1, args, stderr="simulated mkdir failure")
            elif str(module.ROOT / "tools/probe_music_yield.py") in args:
                output = '{"ok":true}'
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "board").mkdir()
            (root / "board/xiaorui_live").write_bytes(b"candidate fixture")
            def path(value):
                return root / "evidence" if str(value).startswith("/tmp/codex-music-") else Path(value)
            with patch.object(module, "ROOT", root), patch.object(module, "Path", path), \
                 patch.object(module.subprocess, "run", run), \
                 patch("sys.argv", ["test", "--apply", "--acoustic", "--serial", "testserial",
                                    "--utt-dump"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = module.main()
            report = json.loads((root / "evidence/summary.json").read_text())
        self.assertEqual(rc, 1)
        self.assertEqual(state["frontend_stops"], 0)
        return state, report

    def test_mkdir_failure_restores_cron_before_frontend_was_touched(self):
        state, report = self.run_failure("mkdir")
        self.assertEqual(state["resumes"], 1)
        self.assertFalse(state["paused"])
        self.assertTrue(report["restored"])

    def test_pause_ack_timeout_still_attempts_resume(self):
        state, report = self.run_failure("pause-ack-lost")
        self.assertEqual(state["resumes"], 1)
        self.assertFalse(state["paused"])
        self.assertTrue(report["restored"])

    def test_cron_still_paused_must_not_report_restored(self):
        state, report = self.run_failure("mkdir", resume_works=False)
        self.assertTrue(state["paused"])
        self.assertFalse(report["restored"])


class HarnessTests(unittest.TestCase):
    def test_generated_board_python_compiles(self):
        tree = ast.parse(SOURCE.read_text())
        values = dict(BASE="/data/test", profile="/data/test/instance", role="test-role",
                      code="/data/test/code", path="/data/test/xiaorui_live")
        count = 0
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "remote" and node.args
                    and isinstance(node.args[0], ast.JoinedStr)):
                script = eval(compile(ast.Expression(node.args[0]), "template", "eval"), values)
                compile(script, "board-python", "exec")
                count += 1
        self.assertGreaterEqual(count, 5)

    def test_audio_ready_does_not_require_a_packet_gap(self):
        source = SOURCE.read_text()
        self.assertIn('and "首包" in t', source)
        self.assertNotIn('and "pkt#" in t', source)

    def test_original_frontend_never_overwritten(self):
        source = SOURCE.read_text()
        self.assertIn('push(ROOT / "board/xiaorui_live", candidate)', source)
        self.assertNotIn('push(ROOT / "board/xiaorui_live", PROD)', source)
        self.assertIn('("resume_cron",', source)
        self.assertIn('("restore_frontend",', source)


if __name__ == "__main__":
    unittest.main()
