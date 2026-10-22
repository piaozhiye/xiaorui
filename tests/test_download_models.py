"""Exercise the real downloader with tiny local archives; never uses the network."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "matcha": ("matcha-icefall-zh-baker/model-steps-3.onnx",
               "ff1cc7f7abdedc4eeec668a4a0dc2e72a0c314c5637d5fce885345b490bd08de"),
    "asr20-rknn": ("sherpa-onnx-rk3576-20-seconds-paraformer-zh-2025-10-07/encoder.rknn",
                   "d9c1e1dc16d62591bdbd025d42322e6770a8266a67fa98c2fad69109fc6b2e1d"),
    "campplus": ("model-voiceprint/campplus.onnx",
                 "f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11"),
}
PAYLOAD = b"tiny model fixture, not an archive"


class DownloadTests(unittest.TestCase):
    def check_case(self, name, archive=None, corrupt=False, missing=False, unpinned=False):
        member, pin = CASES[name]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "bin").mkdir()
            # Keep real pinned production URLs/checksums untouched. Only this
            # disposable script uses a checksum matching our tiny fixture.
            source = (ROOT / "tools/download-models.sh").read_text().replace(
                pin, "" if unpinned else hashlib.sha256(PAYLOAD).hexdigest())
            if archive == "gz":
                source = source.replace(".tar.bz2", ".tar.gz")
            script = root / "tools/download-models.sh"
            script.write_text(source)
            fixture = root / "fixture"
            if corrupt or archive is None:
                fixture.write_bytes(b"bad archive" if corrupt else PAYLOAD)
            else:
                with tarfile.open(fixture, "w:" + archive) as tar:
                    entry = tarfile.TarInfo("unexpected.txt" if missing else member)
                    entry.size = len(PAYLOAD)
                    tar.addfile(entry, io.BytesIO(PAYLOAD))
                    if name == "asr20-rknn":
                        extra = tarfile.TarInfo(str(Path(member).with_name("decoder.rknn")))
                        extra.size = len(PAYLOAD)
                        tar.addfile(extra, io.BytesIO(PAYLOAD))
            wget = root / "bin/wget"
            wget.write_text("""#!/usr/bin/env python3
import os, pathlib, shutil, sys
args=sys.argv[1:]; out=pathlib.Path(args[args.index('-P')+1])
shutil.copyfile(os.environ['DOWNLOAD_FIXTURE'], out/args[-1].rsplit('/',1)[-1])
with open(os.environ['DOWNLOAD_CALLS'],'a') as f: f.write('download\\n')
""")
            wget.chmod(0o755)
            calls = root / "calls"
            env = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                       XR_MODELS_DIR=str(root / "out"), DOWNLOAD_FIXTURE=str(fixture),
                       DOWNLOAD_CALLS=str(calls))
            def run():
                return subprocess.run(["bash", str(script), name], env=env,
                                      capture_output=True, text=True, timeout=10)
            result = run()
            target = root / "out" / member
            if corrupt or missing:
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(target.exists())
            else:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(target.read_bytes(), PAYLOAD)
                self.assertEqual(run().returncode, 0)
                self.assertEqual(calls.read_text().splitlines(), ["download"])
                if name == "asr20-rknn":
                    self.assertTrue(target.with_name("decoder.rknn").is_file())

    def test_matcha_archive_keeps_extracted_model(self):
        self.check_case("matcha", archive="bz2")

    def test_asr_archive_keeps_all_members(self):
        self.check_case("asr20-rknn", archive="bz2")

    def test_gzip_archive(self):
        self.check_case("matcha", archive="gz")

    def test_plain_file_still_moves_to_destination(self):
        self.check_case("campplus")

    def test_corrupt_archive_is_failure(self):
        self.check_case("matcha", archive="bz2", corrupt=True)

    def test_missing_archive_member_is_failure_without_a_pin(self):
        self.check_case("matcha", archive="bz2", missing=True, unpinned=True)


if __name__ == "__main__":
    unittest.main()
