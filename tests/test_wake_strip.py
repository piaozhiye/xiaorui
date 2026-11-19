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

    # 2026-11-19 板端实测(RockASR): 唤醒词会被转成**混合形** ——
    # 第一单元听岔、第二单元是对的("小绿小瑞"/"小看小爱"/"小是小瑞")。
    # 词表里没有这些, 以前只匹到**单字**变体("小绿") -> 只剥 2 字,
    # 剩下"小瑞"落到命令链 -> "转写过短, 回没听清" -> 板子抢答。
    # 正确行为: 匹到 4 字的双单元形态, 剥完剩空 = 纯唤醒 -> 静默。
    def test_mixed_form_strips_four_chars(self):
        for text, word in (
            ("小绿小瑞。", "小绿小瑞"),
            ("小看小爱。", "小看小爱"),
            ("小是小瑞。", "小是小瑞"),
            ("小帅小帅。", "小帅小帅"),
            ("小小瑞。", "小小瑞"),
        ):
            rest, hit = strip_wake_prefix(text, WORDS)
            self.assertIsNotNone(rest, text)
            self.assertEqual(rest, "", f"{text}: 剥完应为空(纯唤醒), 实际 {rest!r}")
            self.assertEqual(hit, word, text)

    def test_mixed_form_with_command_keeps_command(self):
        rest, word = strip_wake_prefix("小绿小瑞现在几点了", WORDS)
        self.assertEqual(rest, "现在几点了")
        self.assertEqual(word, "小绿小瑞")

    def test_normal_sentence_not_mistaken_for_wake(self):
        for text in ("帮我开灯", "小明小刚来了", "今天天气怎么样"):
            rest, word = strip_wake_prefix(text, WORDS)
            self.assertIsNone(rest, text)
            self.assertIsNone(word, text)


if __name__ == "__main__":
    unittest.main()
