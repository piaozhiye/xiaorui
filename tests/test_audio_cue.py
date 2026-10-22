"""Validate cue format/length before allocating or playing external cue files."""
import ctypes
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]

class CueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.base = Path(cls.directory.name)
        shared = cls.base / "cue.so"
        subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        str(ROOT / "board/audio_cue.c"), "-o", str(shared)], check=True)
        cls.lib = ctypes.CDLL(str(shared))
        cls.load = cls.lib.xr_cue_load
        cls.load.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.c_size_t)]
        cls.release = ctypes.CDLL(None).free
        cls.release.argtypes = [ctypes.c_void_p]

    def fixture(self, channels=1, width=2, rate=24000, frames=2880):
        path = self.base / "cue.wav"
        with wave.open(str(path), "wb") as output:
            output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            output.writeframes(b"\0" * (frames * width * channels))
        return path

    def check(self, path, valid, count=2880):
        pointer, samples = ctypes.c_void_p(), ctypes.c_size_t(123)
        result = self.load(str(path).encode(), ctypes.byref(pointer), ctypes.byref(samples))
        try:
            self.assertEqual(result == 0, valid)
            self.assertEqual(samples.value, count if valid else 0)
            self.assertEqual(bool(pointer.value), valid)
        finally:
            self.release(pointer)

    def test_valid_cue(self):
        self.check(self.fixture(), True)

    def test_generated_short_cue(self):
        path = self.base / "beep.wav"
        subprocess.run(["python3", str(ROOT / "tools/gen-wake-cue.py"), str(path)], check=True)
        self.check(path, True)

    def test_wrong_format(self):
        for options in (dict(channels=2), dict(width=1), dict(rate=16000), dict(frames=240001)):
            with self.subTest(options=options):
                self.check(self.fixture(**options), False)

    def test_truncated_or_huge_payload(self):
        path = self.fixture()
        data = path.read_bytes()
        for payload in (data[:20], data[:-1], data[:40] + struct.pack("<I", 0xfffffff0) + data[44:]):
            path.write_bytes(payload)
            self.check(path, False)

if __name__ == "__main__":
    unittest.main()
