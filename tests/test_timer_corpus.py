"""Cold-generation/manifest contract tests, no private model dependency."""
import array
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(REPO / "tools/gen-timer-corpus.py"))


class TimerCorpusTests(unittest.TestCase):
    def test_cold_generation_and_manifest_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            model.mkdir()
            output = root / "tests/audio"
            output.mkdir(parents=True)
            manifest = output / "manifest.json"
            manifest.write_text(json.dumps({"items": [
                {"file": "tests/audio/unrelated.wav"}, {"file": "xr_ck_set1.wav"}]}))
            calls = []
            def synth(args, **kwargs):
                calls.append(args[-2])
                MODULE["write_pcm"](Path(args[-1]), 16000, array.array("h", [1000, -1000] * 1600))
            with patch("subprocess.run", side_effect=synth), patch("sys.argv", [
                "gen", "--root", str(root), "--model-dir", str(model)]):
                MODULE["main"]()
            self.assertEqual(len(calls), 7)  # Includes wake: no untracked input needed.
            items = json.loads(manifest.read_text())["items"]
            self.assertEqual(len(items), 13)
            self.assertFalse(any(item["file"] == "xr_ck_set1.wav" for item in items))
            _, pcm = MODULE["read_pcm"](output / "wake2_ck_set1.wav")
            self.assertEqual(len(pcm), 86400)
            self.assertFalse(any(pcm[3200:83200]))
            MODULE["verify"](root)
            (output / "xr_ck_set1.wav").unlink()
            with self.assertRaises(FileNotFoundError):
                MODULE["verify"](root)


if __name__ == "__main__":
    unittest.main()
