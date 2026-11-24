#!/usr/bin/env python3
"""Exercise FIFO bridge conversion and recovery across snapclient EOF/reconnect."""
from __future__ import annotations

import array
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tools/snapcast-fifo-channel.py"


class SnapcastFifoBridgeTest(unittest.TestCase):
    def test_music_mix_downmixes_both_channels_and_decimates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="snapbridge-mix-test-") as tmp:
            source = pathlib.Path(tmp) / "source.fifo"
            sink = pathlib.Path(tmp) / "sink.fifo"
            os.mkfifo(source)
            os.mkfifo(sink)
            bridge = subprocess.Popen(
                ["python3", str(BRIDGE), str(source), str(sink), "mix"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(self._stop_bridge, bridge)

            stereo = array.array("h")
            expected = array.array("h")
            for frame in range(5003):
                left = frame * 3 - 7000
                right = 6000 - frame
                stereo.extend((left, right))
                if frame % 2 == 0:
                    total = left + right
                    expected.append(total // 2 if total >= 0 else -((-total) // 2))
            received: list[bytes] = []
            errors: list[BaseException] = []

            def read_sink() -> None:
                try:
                    with open(sink, "rb", buffering=0) as stream:
                        received.append(stream.read())
                except BaseException as exc:
                    errors.append(exc)

            def write_source() -> None:
                try:
                    payload = stereo.tobytes()
                    with open(source, "wb", buffering=0) as stream:
                        for offset in range(0, len(payload), 1003):
                            stream.write(payload[offset:offset + 1003])
                            time.sleep(0.001)
                except BaseException as exc:
                    errors.append(exc)

            reader = threading.Thread(target=read_sink, daemon=True)
            writer = threading.Thread(target=write_source, daemon=True)
            reader.start()
            writer.start()
            writer.join(timeout=5)
            reader.join(timeout=5)
            self.assertFalse(writer.is_alive(), "source FIFO writer hung")
            self.assertFalse(reader.is_alive(), "sink FIFO reader hung")
            self.assertEqual(errors, [])
            self.assertEqual(received, [expected.tobytes()])

    def test_two_writer_sessions_reopen_and_preserve_pcm_alignment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="snapbridge-test-") as tmp:
            source = pathlib.Path(tmp) / "source.fifo"
            sink = pathlib.Path(tmp) / "sink.fifo"
            os.mkfifo(source)
            os.mkfifo(sink)
            bridge = subprocess.Popen(
                ["python3", str(BRIDGE), str(source), str(sink), "1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(self._stop_bridge, bridge)

            for session in range(2):
                frames = 4097 + session * 113  # Force multiple reads, not one period.
                stereo = array.array("h")
                for frame in range(frames):
                    stereo.extend((frame - 2000, 12000 - frame))
                payload = stereo.tobytes()
                expected = array.array("h", stereo[1::2][::2]).tobytes()
                received: list[bytes] = []
                errors: list[BaseException] = []

                def read_sink() -> None:
                    try:
                        with open(sink, "rb", buffering=0) as stream:
                            received.append(stream.read())
                    except BaseException as exc:  # surface thread failures in the test
                        errors.append(exc)

                def write_source() -> None:
                    try:
                        with open(source, "wb", buffering=0) as stream:
                            # Deliberately split at non-frame boundaries; the bridge must
                            # carry trailing bytes across reads and reset at EOF.
                            offsets = (1, 2, 7, 1021, 4099, len(payload))
                            start = 0
                            for end in offsets:
                                end = min(end, len(payload))
                                if end > start:
                                    stream.write(payload[start:end])
                                    start = end
                                    time.sleep(0.002)
                            if start < len(payload):
                                stream.write(payload[start:])
                    except BaseException as exc:
                        errors.append(exc)

                reader = threading.Thread(target=read_sink, daemon=True)
                writer = threading.Thread(target=write_source, daemon=True)
                reader.start()
                writer.start()
                writer.join(timeout=5)
                reader.join(timeout=5)
                self.assertFalse(writer.is_alive(), "source FIFO writer hung")
                self.assertFalse(reader.is_alive(), "sink FIFO reader hung")
                self.assertEqual(errors, [])
                first_diff = next((i for i, pair in enumerate(zip(received[0], expected))
                                   if pair[0] != pair[1]), None)
                self.assertEqual(received, [expected],
                                 f"session={session} got={len(received[0])} expected={len(expected)} "
                                 f"first_diff={first_diff} got_prefix={array.array('h', received[0][:24])} "
                                 f"expected_prefix={array.array('h', expected[:24])}")
                self.assertIsNone(bridge.poll(), "bridge exited after FIFO EOF")

    @staticmethod
    def _stop_bridge(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
