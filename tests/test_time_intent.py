#!/usr/bin/env python3
"""报时意图单测: 封闭短语匹配(宁漏不抢) + 口语时间形。"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.timeIntent import match_time_query, spoken_now  # noqa: E402

POSITIVES = [
    "现在几点了", "几点了", "现在几点", "几点", "几点钟", "报时",
    "现在什么时间", "报一下时间", "现在几点了？", "几点了。",
]
NEGATIVES = [
    "从北京到上海要几点",       # 含"几点"的真问题
    "几点吃饭合适",
    "昨天几点了",
    "现在几点开会",
    "介绍一下人工智能的发展历史",
    "",
    "播放音乐",
    "明天几点叫我起床",          # 定时(时刻制) -> 放行
]


class TimeIntentTests(unittest.TestCase):
    def test_match(self):
        for t in POSITIVES:
            self.assertTrue(match_time_query(t), t)
        for t in NEGATIVES:
            self.assertFalse(match_time_query(t), t)

    def test_spoken(self):
        tz = datetime.timezone(datetime.timedelta(hours=8))
        cases = [
            (datetime.datetime(2026, 10, 16, 3, 5, tzinfo=tz), "现在是凌晨三点零五分"),
            (datetime.datetime(2026, 10, 16, 9, 0, tzinfo=tz), "现在是上午九点整"),
            (datetime.datetime(2026, 10, 16, 11, 45, tzinfo=tz), "现在是上午十一点四十五分"),
            (datetime.datetime(2026, 10, 16, 12, 30, tzinfo=tz), "现在是中午十二点半".replace("半", "三十分")),
            (datetime.datetime(2026, 10, 16, 15, 5, tzinfo=tz), "现在是下午三点零五分"),
            (datetime.datetime(2026, 10, 16, 20, 0, tzinfo=tz), "现在是晚上八点整"),
            (datetime.datetime(2026, 10, 16, 0, 10, tzinfo=tz), "现在是凌晨十二点十分"),
        ]
        for now, want in cases:
            self.assertEqual(spoken_now(now), want, now)


if __name__ == "__main__":
    unittest.main()
