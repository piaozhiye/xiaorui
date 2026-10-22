#!/usr/bin/env python3
"""下一首/换台意图匹配单测。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.nextIntent import match_next_query  # noqa: E402


class NextIntentTests(unittest.TestCase):
    def test_match(self):
        for t in ["下一首", "下一曲", "再来一首", "换一首", "切歌",
                  "换个电台", "换台", "换一个电台。", "下一个电台"]:
            self.assertTrue(match_next_query(t), t)
        for t in ["下一首歌叫什么",       # 问句含"下一首"子串 -> 放行
                  "换一个话题",
                  "播放音乐", "", "现在几点了", "再来一杯"]:
            self.assertFalse(match_next_query(t), t)


if __name__ == "__main__":
    unittest.main()
