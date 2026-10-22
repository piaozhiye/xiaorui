#!/usr/bin/env python3
"""电台意图匹配单测(动词门/剥壳/模糊/不盲选)。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.radioIntent import match_radio as _match_radio  # noqa: E402

RADIOS = [
    {"name": "轻音乐电台", "url": "http://x/groove", "radio": True},
    {"name": "电子音乐电台", "url": "http://x/beat", "radio": True},
    {"name": "爵士电台", "url": "http://x/jazz", "radio": True},
]


class RadioIntentTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(_match_radio(RADIOS, "播放轻音乐电台")["name"], "轻音乐电台")
        self.assertEqual(_match_radio(RADIOS, "小瑞小瑞听爵士电台")["name"], "爵士电台")
        self.assertEqual(_match_radio(RADIOS, "打开电子音乐电台")["name"], "电子音乐电台")
        self.assertEqual(_match_radio(RADIOS, "放个电台")["name"], "轻音乐电台")
        self.assertEqual(_match_radio(RADIOS, "换个广播听听")["name"], "轻音乐电台")

    def test_negative_chat_not_intercepted(self):
        self.assertIsNone(_match_radio(RADIOS, "广播是怎么发明的"))
        self.assertIsNone(_match_radio(RADIOS, "电台的历史讲一下"))
        self.assertIsNone(_match_radio(RADIOS, "播放假想的电台好了"))
        self.assertIsNone(_match_radio(RADIOS, "现在几点了"))
        self.assertIsNone(_match_radio(RADIOS, ""))

    def test_bare_station_request_without_verb(self):
        # 无动词的点台请求也要接住: 放走了就落到意图 LLM, 而 LLM 会把
        # "电台"字样吞成 song_name=random(实测), 电台链路当场变死路。
        self.assertEqual(_match_radio(RADIOS, "畅音电台")["name"], "轻音乐电台")
        self.assertEqual(_match_radio(RADIOS, "轻音乐电台")["name"], "轻音乐电台")
        # 不提"电台/广播"就不算点台(可能是在说歌)
        self.assertIsNone(_match_radio(RADIOS, "爵士"))
        # 疑问句/长句仍然放行给 LLM
        self.assertIsNone(_match_radio(RADIOS, "畅音电台是怎么发明的"))
        self.assertIsNone(_match_radio(RADIOS, "帮我搜一下关于爵士电台的历史资料"))


if __name__ == "__main__":
    unittest.main()
