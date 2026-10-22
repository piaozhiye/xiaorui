#!/usr/bin/env python3
"""唤醒词前缀剥离(一句式"小瑞小瑞帮我开灯")与大小写不敏感。

2026-10-20: 拼音形态(xiaorui)的大小写由 ASR 决定, 只在配置里列小写一种写法,
所以比较必须大小写不敏感。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server" / "pkg"))

from core.handle.intentHandler import strip_wake_prefix  # noqa: E402

WORDS = ["小瑞小瑞", "小瑞", "小绿小绿", "你好小瑞", "你好xiaorui", "xiaorui"]


class StripWakePrefixTests(unittest.TestCase):
    def test_strips_chinese_wake_phrase(self):
        rest, word = strip_wake_prefix("小瑞小瑞帮我开灯", WORDS)
        self.assertEqual(rest, "帮我开灯")
        self.assertEqual(word, "小瑞小瑞")

    def test_strips_misheard_variant(self):
        rest, word = strip_wake_prefix("小绿小绿帮我关灯", WORDS)
        self.assertEqual(rest, "帮我关灯")
        self.assertEqual(word, "小绿小绿")

    def test_longest_wake_phrase_wins(self):
        rest, word = strip_wake_prefix("小瑞小瑞小瑞", WORDS)
        self.assertEqual(rest, "小瑞")
        self.assertEqual(word, "小瑞小瑞")

    def test_pinyin_is_case_insensitive(self):
        for text in ("Xiaorui帮我开灯", "XIAORUI帮我开灯", "你好Xiaorui帮我开灯",
                     "你好XIAORUI帮我开灯"):
            rest, word = strip_wake_prefix(text, WORDS)
            self.assertIsNotNone(rest, text)
            self.assertEqual(rest, "帮我开灯", text)

    def test_no_wake_phrase(self):
        rest, word = strip_wake_prefix("帮我开灯", WORDS)
        self.assertIsNone(rest)
        self.assertIsNone(word)


if __name__ == "__main__":
    unittest.main()
