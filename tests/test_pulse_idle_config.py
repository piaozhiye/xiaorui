import importlib.util
from pathlib import Path
import contextlib
import io
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "ports/rk3562/patch-pulse-idle.py"
spec = importlib.util.spec_from_file_location("patch_pulse_idle", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PulseIdleConfigTests(unittest.TestCase):
    def test_replaces_default_timeout_and_is_idempotent(self):
        source = "### suspend idle devices\nload-module module-suspend-on-idle\n"
        expected = "### suspend idle devices\nload-module module-suspend-on-idle timeout=0\n"
        self.assertEqual(module.patched_text(source), expected)
        self.assertEqual(module.patched_text(expected), expected)

    def test_replaces_existing_timeout_and_collapses_duplicates(self):
        source = "load-module module-suspend-on-idle timeout=5\nload-module module-suspend-on-idle timeout=3\n"
        self.assertEqual(module.patched_text(source), "load-module module-suspend-on-idle timeout=0\n")

    def test_preserves_comments_and_appends_when_missing(self):
        source = "# load-module module-suspend-on-idle timeout=5\nload-module module-alsa-sink device=default\n"
        expected = source + "load-module module-suspend-on-idle timeout=0\n"
        self.assertEqual(module.patched_text(source), expected)

    def test_write_is_idempotent_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "default.pa"
            path.write_text("load-module module-suspend-on-idle\n", encoding="utf-8")
            path.chmod(0o640)
            with patch.object(sys, "argv", [str(SCRIPT), str(path), "--write"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "load-module module-suspend-on-idle timeout=0\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(Path(str(path) + ".pre-xiaorui").read_text(encoding="utf-8"), "load-module module-suspend-on-idle\n")
            with patch.object(sys, "argv", [str(SCRIPT), str(path), "--write"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(), 0)
            self.assertEqual(module.patched_text(path.read_text(encoding="utf-8")), path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
