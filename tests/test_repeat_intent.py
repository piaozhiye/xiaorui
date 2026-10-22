#!/usr/bin/env python3
"""'再说一遍'意图匹配单测。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.repeatIntent import match_repeat_query  # noqa: E402


class RepeatIntentTests(unittest.TestCase):
    def test_match(self):
        for t in ["再说一遍", "再讲一遍", "再说一次", "重复一遍", "你再说一遍",
                  "再说一遍。", "再说一遍好吗"]:
            self.assertTrue(match_repeat_query(t), t)
        for t in ["把故事再讲一遍",     # 指代前文 -> LLM
                  "大声一点再讲一遍",   # 复合指令 -> LLM
                  "现在几点了", "", "播放音乐", "取消提醒"]:
            self.assertFalse(match_repeat_query(t), t)


if __name__ == "__main__":
    unittest.main()
