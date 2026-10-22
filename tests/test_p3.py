"""p3 容器解码回归: 流式回调版与整读版帧序列必须一致。

背景: tts/base 的本地 .p3 FILE 路径调用 decode_opus_from_file_stream,
该函数曾缺失(AttributeError), 本地歌曲轮静默中断 —— 在线歌单可用时
遮蔽此 bug, 候选实例(无 music-online)首次暴露。
"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server/pkg"))
from core.utils import p3


def _build_p3(frames):
    blob = b""
    for i, frame in enumerate(frames):
        blob += struct.pack(">BBH", 0, 0, len(frame)) + frame
    return blob


class P3StreamTests(unittest.TestCase):
    def test_stream_matches_bulk(self):
        frames = [bytes([i % 251] * (37 + i)) for i in range(120)]
        with tempfile.NamedTemporaryFile(suffix=".p3") as f:
            f.write(_build_p3(frames))
            f.flush()
            streamed = []
            p3.decode_opus_from_file_stream(f.name, streamed.append)
            bulk, duration = p3.decode_opus_from_file(f.name)
        self.assertEqual(streamed, bulk)
        self.assertEqual(streamed, frames)
        self.assertAlmostEqual(duration, 120 * 0.06)

    def test_truncated_frame_raises(self):
        blob = _build_p3([b"\x01" * 50])[:-10]
        with tempfile.NamedTemporaryFile(suffix=".p3") as f:
            f.write(blob)
            f.flush()
            with self.assertRaises(ValueError):
                p3.decode_opus_from_file_stream(f.name, lambda _: None)


if __name__ == "__main__":
    unittest.main()
