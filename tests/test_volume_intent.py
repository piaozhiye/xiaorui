#!/usr/bin/env python3
"""服务端音量意图判定单测(与 board/xr_volume.c 的 C 表互为镜像)。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.volumeIntent import match_volume_intent  # noqa: E402

POSITIVES = [
    ("大声一点", "up", 0),
    ("大点声", "up", 0),
    ("声音大一点", "up", 0),
    ("请把声音调大一点", "up", 0),
    ("响一点", "up", 0),
    ("再大点", "up", 0),
    ("小声一点", "down", 0),
    ("小点声", "down", 0),
    ("调小一点", "down", 0),
    ("声音小一点", "down", 0),
    ("轻一点", "down", 0),
    ("太吵了", "down", 0),
    ("太大声了", "down", 0),
    ("音量小点吧", "down", 0),
    ("静音", "min", 0),
    ("别出声", "min", 0),
    ("最小声", "min", 0),
    ("最大声", "max", 0),
    ("音量调到最大", "max", 0),
    ("音量调到百分之六十", "set", 60),
    ("音量百分之三十", "set", 30),
    ("百分之五十", "set", 50),
    ("音量50", "set", 50),
    ("音量百分之百", "set", 100),
    ("把音量开到百分之八十", "set", 80),
    ("音量六十", "set", 60),
    ("声音调小到百分之二十", "set", 20),
]
NEGATIVES = [
    "大声唱首歌",
    "讲一个大点声的故事",
    "五十六",
    "把窗户打开一点",
    "今天天气怎么样",
    "大点声唱歌好吗",
    "音量",
    "小点声唱个摇篮曲",
    "声音大一点再讲个故事",
    "大声朗读这首诗",
    "帮我打开百分之五十亮度的灯",
    "小明你好",
    "",
    "这个音量控制是谁教你的呀",
    "一百八十八号房间在哪里",
]


class VolumeIntentTests(unittest.TestCase):
    def test_positives(self):
        for text, op, value in POSITIVES:
            got = match_volume_intent(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got["op"], op, text)
            if op == "set":
                self.assertEqual(got["value"], value, text)

    def test_negatives_must_pass_through(self):
        for text in NEGATIVES:
            self.assertIsNone(match_volume_intent(text), text)

    def test_cjk_number_forms(self):
        for text, want in [("百分之十", 10), ("百分之二十", 20),
                           ("音量七十", 70), ("音量一百", 100), ("音量30", 30)]:
            got = match_volume_intent(text)
            self.assertEqual((got or {}).get("value"), want, text)


if __name__ == "__main__":
    unittest.main()
