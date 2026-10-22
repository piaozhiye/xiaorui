#!/usr/bin/env python3
"""声纹意图匹配单测(注册名字解析 + 我是谁封闭集)。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.voiceprintIntent import (  # noqa: E402
    match_voiceprint_enroll, match_who_am_i)


class VoiceprintIntentTests(unittest.TestCase):
    def test_enroll_name(self):
        self.assertEqual(match_voiceprint_enroll("记住我的声音，我叫小明"), "小明")
        self.assertEqual(match_voiceprint_enroll("小瑞小瑞记住我的声音我叫小罗"), "小罗")
        self.assertEqual(match_voiceprint_enroll("记住我的声音，我是爸爸"), "爸爸")
        self.assertEqual(match_voiceprint_enroll("帮我注册声纹名字叫妈妈"), "妈妈")
        self.assertEqual(match_voiceprint_enroll("记住我的声音"), "")   # 没解析到名字

    def test_enroll_miss(self):
        for t in ["记住我的声音再说一遍",     # 无名+非注册语义边界 -> 名字解析空串=提示
                  "大声一点",
                  "我是谁",
                  "播放音乐",
                  ""]:
            # 空串也算命中(给提示), 但这些不含注册字样的必须 None
            if "记住我的声音" not in t and "注册声纹" not in t:
                self.assertIsNone(match_voiceprint_enroll(t), t)

    def test_who_am_i(self):
        for t in ["我是谁", "我是谁？", "你知道我是谁吗", "认得我吗", "认识我吗"]:
            self.assertTrue(match_who_am_i(t), t)
        for t in ["我是谁的母亲", "你是谁", "记住我的声音我叫小明", "现在几点了"]:
            self.assertFalse(match_who_am_i(t), t)


if __name__ == "__main__":
    unittest.main()
