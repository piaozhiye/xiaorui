#!/usr/bin/env python3
"""定时提醒意图判定单测(时长形式/宁漏不抢)。"""
import sys
import unittest
import datetime
import asyncio
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.timerIntent import (  # noqa: E402
    match_timer_intent, match_clock_alarm, match_timer_list_query)

POSITIVES = [
    ("帮我设一个三十秒的计时器", "set", 30),
    ("定一个十分钟的闹钟", "set", 600),
    ("设个五分钟的提醒", "set", 300),
    ("一分钟后再提醒我", "set", 60),
    ("一分钟后提醒我", "set", 60),
    ("两分钟后叫我", "set", 120),
    ("半小时后提醒我", "set", 1800),
    ("设置一个两小时的定时", "set", 7200),
    ("来一个45秒的倒计时", "set", 45),
    ("设一个两分半的计时器", "set", 150),
    ("取消提醒", "cancel", None),
    ("取消闹钟", "cancel", None),
    ("把计时器取消一下", "cancel", None),
    ("取消三十秒的提醒", "cancel", 30),
]
NEGATIVES = [
    "讲一个十分钟的睡前故事",     # 故事不是定时
    "十分钟有多长",               # 提问
    "现在几点了",                 # 报时
    "大声唱首歌",
    "声音小一点",
    "明早八点叫我起床",           # 时刻制(v1 不支持) -> 放行 LLM
    "这是一个十分钟的闹钟吗",       # 问句(吗残留) -> 放行
    "十分钟够吗",
    "设一个三天的提醒",           # 超上限(12h)
    "计时器怎么用",
    "",
    "音量调到最大",
]


class TimerIntentTests(unittest.TestCase):
    def test_positives(self):
        for text, op, seconds in POSITIVES:
            got = match_timer_intent(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got["op"], op, text)
            if op == "set":
                self.assertEqual(got["seconds"], seconds, text)
            elif seconds is not None:
                self.assertEqual(got["seconds"], seconds, text)

    def test_negatives_pass_through(self):
        for text in NEGATIVES:
            self.assertIsNone(match_timer_intent(text), text)


class FrozenClockTests(unittest.TestCase):
    def setUp(self):
        fixed = datetime.datetime(2026, 10, 16, 11, 0,
                                  tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        class Clock(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz else fixed.replace(tzinfo=None)
        clock = patch("datetime.datetime", Clock)
        clock.start()
        self.addCleanup(clock.stop)


class ClockAlarmTests(FrozenClockTests):
    """时刻制: 只验解析形态与往返秒数合理性(绝对时刻随当前时间变)。"""

    def test_forms(self):
        for t in ["明早八点叫我起床", "明晚九点的提醒", "明天下午三点提醒我",
                  "早上七点半叫我", "今晚十一点提醒我", "晚上十点闹钟",
                  "中午十二点闹钟", "八点叫我", "两点半提醒我"]:
            r = match_clock_alarm(t)
            self.assertIsNotNone(r, t)
            self.assertEqual(r["op"], "set", t)
            self.assertTrue(60 <= r["seconds"] <= 48 * 3600, t)
            self.assertTrue(r["speak"], t)

    def test_miss(self):
        for t in ["现在几点了", "大声一点", "帮我设一个三十秒的计时器",
                  "明早吃什么呢", "八点钟方向", "播放音乐"]:
            self.assertIsNone(match_clock_alarm(t), t)

    def test_list_query(self):
        for t in ["有什么提醒", "我设了什么提醒", "闹钟列表", "还有几个提醒"]:
            self.assertTrue(match_timer_list_query(t), t)
        for t in ["播放音乐", "现在几点了", ""]:
            self.assertFalse(match_timer_list_query(t), t)


class ClockCancelTests(FrozenClockTests):
    """否定词在时刻短语之前 => 取消语义, 绝不能建新闹钟(review P1 实测)。"""

    def test_cancel_not_set(self):
        for t in ["取消明早八点闹钟", "不要设置下午三点的闹钟",
                  "帮我取消明天早上七点半的提醒", "取消8:24a.m.的闹钟",
                  "把今晚十点的闹钟删掉", "今晚十点的闹钟取消一下",
                  "不要设置，下午三点的闹钟", "别设了，明早八点的闹钟"]:
            r = match_clock_alarm(t)
            self.assertIsNotNone(r, t)
            self.assertEqual(r["op"], "cancel_clock", t)
            self.assertIsNotNone(r.get("h24"), t)

    def test_negation_after_phrase_still_sets(self):
        # 否定词指别的事务, 不是取消闹钟
        r = match_clock_alarm("明早八点叫我别忘了关窗")
        self.assertIsNotNone(r)
        self.assertEqual(r["op"], "set")
        r = match_clock_alarm("明早八点提醒我取消会议")
        self.assertIsNotNone(r)
        self.assertEqual(r["op"], "set")


class CancelCoexistenceTests(FrozenClockTests):
    """多提醒共存的定点取消(2026-10-16 二轮 review: 早晚互删/时长清全库)。"""

    def setUp(self):
        super().setUp()
        from core.handle import timerIntent as TI
        self.TI = TI
        TI._TIMERS.clear()
        self.addCleanup(TI._TIMERS.clear)

    def _entry(self, name, kind, speak, h24=None, minute=None, dur=None, target=None):
        if kind == "clock" and target is None:
            target = self.TI._clock_target(h24, minute, False)[1]
        self.TI._TIMERS[name] = {
            "task": None, "speak": speak, "kind": kind, "h24": h24,
            "minute": minute, "dur_seconds": dur, "due": 0, "target_at": target}

    def test_same_time_different_dates(self):
        for name, text in [("tonight", "今晚八点闹钟"), ("tomorrow", "明晚八点闹钟")]:
            intent = match_clock_alarm(text)
            self._entry(name, "clock", intent["speak"], 20, 0, target=intent["target_at"])
        for text, expected in [("取消今晚八点闹钟", "tonight"),
                               ("取消明晚八点闹钟", "tomorrow")]:
            self.assertEqual(self.TI._pick_cancel_targets(match_clock_alarm(text)), [expected])

    def test_invalid_duration_never_cancel_all(self):
        self._entry("keep", "duration", "三十秒", dur=30)
        for text in ["取消五秒的提醒", "取消十三小时的提醒", "取消999秒的提醒"]:
            intent = match_timer_intent(text)
            self.assertEqual(intent["op"], "invalid_cancel", text)
            self.assertEqual(self.TI._pick_cancel_targets(intent), [], text)

    def test_explicit_today_cancel_does_not_roll_to_tomorrow(self):
        tomorrow = match_clock_alarm("明早八点闹钟")
        self._entry("keep", "clock", tomorrow["speak"], 8, 0, target=tomorrow["target_at"])
        self.assertEqual(self.TI._pick_cancel_targets(
            match_clock_alarm("取消今天早上八点闹钟")), [])

    def test_plain_tomorrow_preserves_date(self):
        intent = match_clock_alarm("明天八点闹钟")
        target = datetime.datetime.fromtimestamp(intent["target_at"],
            datetime.timezone(datetime.timedelta(hours=8)))
        self.assertEqual((target.day, target.hour), (17, 8))

    def test_clock_cancel_keeps_other_period(self):
        # 早上八点(8:00) 与 晚上八点(20:00) 共存: 文本归一会互删, h24 不会
        self._entry("a", "clock", "早上八点", h24=8, minute=0)
        self._entry("b", "clock", "晚上八点", h24=20, minute=0)
        intent = match_clock_alarm("取消早上八点闹钟")
        self.assertEqual(intent["op"], "cancel_clock")
        self.assertEqual(intent["h24"], 8)
        self.assertEqual(self.TI._pick_cancel_targets(intent), ["a"])

    def test_clock_cancel_cross_format(self):
        # 中文式设的闹钟, 冒号式点名取消(h24 一致即匹配)
        self._entry("a", "clock", "下午三点三十分", h24=15, minute=30)
        intent = match_clock_alarm("取消下午3:30的闹钟")
        self.assertEqual(intent["h24"], 15)
        self.assertEqual(self.TI._pick_cancel_targets(intent), ["a"])

    def test_duration_cancel_keeps_other_duration(self):
        self._entry("a", "duration", "三十秒", dur=30)
        self._entry("b", "duration", "1小时", dur=3600)
        intent = match_timer_intent("取消三十秒的提醒")
        self.assertEqual(intent["op"], "cancel")
        self.assertEqual(intent["seconds"], 30)
        self.assertEqual(self.TI._pick_cancel_targets(intent), ["a"])

    def test_clock_and_duration_independent(self):
        # 同为 30: 时长 30 秒 与 30 分钟的时钟互不干扰
        self._entry("a", "duration", "三十秒", dur=30)
        self._entry("b", "clock", "凌晨零点半", h24=0, minute=30)
        intent = match_timer_intent("取消三十秒的提醒")
        self.assertEqual(self.TI._pick_cancel_targets(intent), ["a"])
        intent = match_clock_alarm("取消凌晨零点半的闹钟")
        self.assertEqual(self.TI._pick_cancel_targets(intent), ["b"])

    def test_cancel_all_when_unspecified(self):
        self._entry("a", "duration", "三十秒", dur=30)
        self._entry("b", "clock", "明早八点", h24=8, minute=0)
        self.assertIsNone(self.TI._pick_cancel_targets({"op": "cancel"}))


class TimerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from core.handle import timerIntent as timers
        self.timers = timers
        timers._TIMERS.clear()
        self.speech = MagicMock()
        self.conn = SimpleNamespace(logger=MagicMock())
        modules = patch.dict(sys.modules, {
            "core.handle.sendAudioHandle": SimpleNamespace(send_stt_message=AsyncMock()),
            "core.handle.intentHandler": SimpleNamespace(speak_txt=self.speech),
        })
        modules.start()
        self.addCleanup(modules.stop)

    async def asyncTearDown(self):
        tasks = [item["task"] for item in self.timers._TIMERS.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.timers._TIMERS.clear()

    async def test_store_and_cancel_actual_date(self):
        for target in (1792152000, 1792238400):
            await self.timers.handle_timer_intent(self.conn, "set", {
                "op": "set", "seconds": 3600, "speak": "晚上八点",
                "h24": 20, "minute": 0, "target_at": target})
        self.assertEqual(len(self.timers._TIMERS), 2)
        await self.timers.handle_timer_intent(self.conn, "cancel", {
            "op": "cancel_clock", "h24": 20, "minute": 0, "target_at": 1792152000})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        remaining = list(self.timers._TIMERS.values())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["target_at"], 1792238400)

    async def test_invalid_cancel_preserves_real_task(self):
        await self.timers.handle_timer_intent(self.conn, "set", {
            "op": "set", "seconds": 3600, "speak": "一小时"})
        task = next(iter(self.timers._TIMERS.values()))["task"]
        for text in ("取消五秒的提醒", "取消十三小时的提醒", "取消999秒的提醒"):
            await self.timers.handle_timer_intent(self.conn, text, match_timer_intent(text))
            self.assertEqual(len(self.timers._TIMERS), 1)
            self.assertFalse(task.cancelling())
        await self.timers.handle_timer_intent(self.conn, "取消提醒", match_timer_intent("取消提醒"))
        self.assertTrue(task.cancelling())
        await asyncio.gather(task, return_exceptions=True)


class ClockColonTests(FrozenClockTests):
    """冒号式换算: 直接断言目标 wall-clock(UTC+8), 不再走中文回灌。"""

    def _target(self, text):
        import datetime
        r = match_clock_alarm(text)
        self.assertIsNotNone(r, text)
        self.assertEqual(r["op"], "set", text)
        tz = datetime.timezone(datetime.timedelta(hours=8))
        now = datetime.datetime.now(tz)
        target = datetime.datetime.fromtimestamp(r["target_at"], tz)
        self.assertEqual(r["seconds"], int((target - now).total_seconds()))
        return target, now

    def test_am_midnight(self):
        # 12:30a.m. = 00:30, 曾被排到中午 12:30
        target, _ = self._target("12:30a.m.闹钟")
        self.assertEqual((target.hour, target.minute), (0, 30))

    def test_pm_afternoon_keeps_afternoon(self):
        # 明天下午3:30p.m. = 明天 15:30, 曾被折成明早 3:30
        target, now = self._target("明天下午3:30p.m.提醒我")
        self.assertEqual((target.hour, target.minute), (15, 30))
        self.assertGreater(target.date(), now.date())

    def test_pm_noon(self):
        target, _ = self._target("12:30p.m.提醒我")
        self.assertEqual((target.hour, target.minute), (12, 30))

    def test_chinese_period_no_ampm(self):
        # "下午3:30提醒我"(无 a.m./p.m.): 按中文时段换算, 曾被排到凌晨
        target, _ = self._target("下午3:30提醒我")
        self.assertEqual((target.hour, target.minute), (15, 30))

    def test_tomorrow_chinese_period_no_ampm(self):
        target, now = self._target("明天下午3:30提醒我")
        self.assertEqual((target.hour, target.minute), (15, 30))
        self.assertGreater(target.date(), now.date())

    def test_evening_and_morning_colon(self):
        target, _ = self._target("晚上8:00提醒我")
        self.assertEqual((target.hour, target.minute), (20, 0))
        target, _ = self._target("早上8:24提醒我")
        self.assertEqual((target.hour, target.minute), (8, 24))

    def test_speak_phrase_readable(self):
        r = match_clock_alarm("明天下午3:30p.m.提醒我")
        self.assertEqual(r["speak"], "明天下午三点半")
        r = match_clock_alarm("8:24a.m.的闹钟")
        self.assertEqual(r["speak"], "早上八点二十四分")


if __name__ == "__main__":
    unittest.main()
