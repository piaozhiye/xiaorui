import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FEEDER = ROOT / "tools" / "mic-feed.py"


class MicFeedTests(unittest.TestCase):
    def test_appends_frame_aligned_tail_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.raw"
            fifo = root / "mic.fifo"
            source.write_bytes(b"\x01\x02\x03\x04")
            import os

            os.mkfifo(fifo)
            feeder = subprocess.Popen(
                [sys.executable, str(FEEDER), str(source), str(fifo), "1", "0", "100"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                with fifo.open("rb", buffering=0) as reader:
                    chunks = []
                    size = 0
                    while size < 6400:
                        block = reader.read(6400 - size)
                        if not block:
                            break
                        chunks.append(block)
                        size += len(block)
                    stream = b"".join(chunks)
                stdout, stderr = feeder.communicate(timeout=3)
            finally:
                if feeder.poll() is None:
                    feeder.kill()
                    feeder.wait()

            self.assertEqual(feeder.returncode, 0, stderr)
            self.assertEqual(stream[:4], b"\x01\x02\x03\x04")
            self.assertEqual(stream[4:], bytes(6396))
            self.assertIn("tail_bytes=6396", stdout)
            self.assertIn("feed_done loops=1 bytes=6400/6400", stdout)


if __name__ == "__main__":
    unittest.main()
