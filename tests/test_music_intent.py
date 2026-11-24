#!/usr/bin/env python3
"""放歌意图匹配单测(规则门/剥壳/控制句与闲聊不拦)。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.musicIntent import extract_song, match_music  # noqa: E402


class MusicIntentTests(unittest.TestCase):
    def test_positive_generic_request(self):
        # 泛指 -> 空串(调用方按随机处理)
        self.assertEqual(match_music("播放音乐"), "")
        self.assertEqual(match_music("放首歌"), "")
        self.assertEqual(match_music("来首歌"), "")
        self.assertEqual(match_music("我要听歌"), "")
        self.assertEqual(match_music("放一首音乐"), "")

    def test_positive_named_song(self):
        self.assertEqual(match_music("播放演示曲"), "演示曲")
        self.assertEqual(match_music("播放周杰伦"), "周杰伦")
        self.assertEqual(match_music("播放一首周杰伦"), "周杰伦")
        self.assertEqual(match_music("帮我放首歌叫晴天"), "晴天")

    def test_genre_word_kept_whole(self):
        # 剥壳不能把"轻音乐"削成"轻"
        self.assertEqual(match_music("播放轻音乐"), "轻音乐")
        self.assertEqual(match_music("来点爵士"), "爵士")
        self.assertEqual(match_music("播放助眠音乐"), "助眠")

    def test_control_sentences_not_intercepted(self):
        # 播放期控制句归 next/音量意图, 不能当成新的放歌请求
        self.assertIsNone(match_music("下一首"))
        self.assertIsNone(match_music("换一首"))
        self.assertIsNone(match_music("停止播放音乐"))
        self.assertIsNone(match_music("别再放歌了"))
        self.assertIsNone(match_music("暂停"))
        self.assertIsNone(match_music("音乐太吵了"))
        self.assertIsNone(match_music("声音小一点"))

    def test_chat_not_intercepted(self):
        self.assertIsNone(match_music("音乐播放器怎么打开"))
        self.assertIsNone(match_music("推荐几首音乐"))
        self.assertIsNone(match_music("音乐是什么"))
        self.assertIsNone(match_music("现在几点了"))
        self.assertIsNone(match_music(""))

    def test_daily_words_not_mistaken_for_song(self):
        # 软动词 + 没有音乐名词: 日常话不能当歌名
        self.assertIsNone(match_music("来杯水"))
        self.assertIsNone(match_music("来点外卖"))
        # 剥出来太长不像歌名
        self.assertIsNone(match_music("播放一段非常长的关于音乐的历史"))

    def test_extract_song_helper(self):
        self.assertEqual(extract_song("播放演示曲"), "演示曲")
        self.assertEqual(extract_song("播放音乐"), "")
        self.assertEqual(extract_song("放"), "")


if __name__ == "__main__":
    unittest.main()
