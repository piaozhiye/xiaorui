#!/usr/bin/env python3
"""唤醒词"形状"判定(小X小Y)—— 词表之外的转写渲染。

2026-10-20 板端实测: 同一句"小瑞小瑞", 三条链路的转写各不相同 ——
  本地 RKNN:   "小想" / "小看看看"
  云 ASR:      "小度小度"(ASR 按"常见助手名"做了语义偏好)
  理想:        "小瑞小瑞"
只靠枚举词表必然追不上; 而"小X小Y"这个形状是稳定的。形状判定只用于
*纯唤醒*与*一句式前缀*两种情况, 不改变"必须带尾巴才算指令"的语义:
  "小度小度"      -> 纯唤醒(静默 + 开跟说窗)
  "小度小度开灯"  -> 剥前缀 -> "开灯" 走意图链
  "小度"          -> 不是唤醒(单单元, 可能是人名/普通词)
  "小明小刚来了"  -> 形状命中但带尾巴 -> 只剥前缀, 不会闲聊
"""
import sys
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server" / "pkg"))

from core.handle.intentHandler import match_wake_shape, strip_wake_prefix  # noqa: E402
from core.handle import intentHandler  # noqa: E402


class WakeShapeTests(unittest.TestCase):
    def test_guarded_leading_fillers(self):
        for text, tail in (("为小鹿小鹿帮我的忙", "帮我的忙"),
                           ("喂小瑞小瑞现在几点了", "现在几点了"),
                           ("喂小月小月现在几点了", "现在几点了"),
                           ("为小月小月现在几点了", "现在几点了"),
                           ("谢谢小度小度晚安", "晚安"),
                           ("哎小瑞小瑞", ""), ("嗯小瑞小瑞开灯", "开灯")):
            self.assertEqual(match_wake_shape(text), tail, text)

    def test_arbitrary_prefixes_are_not_wakes(self):
        for text in ("不是小度小度", "别叫小度小度", "他说小度小度",
                     "我认识小度小度", "谢谢你小度小度", "为啥小度小度",
                     "喂你小瑞小瑞", "别喂小瑞小瑞", "喂小明小刚"):
            self.assertIsNone(match_wake_shape(text), text)

    def test_double_unit_is_wake(self):
        for text in ("小度小度", "小想小想", "小瑞小瑞", "小丽小瑞"):
            self.assertIsNotNone(match_wake_shape(text), text)

    def test_single_leading_xiao_is_not_wake(self):
        """本地 RKNN 的"小看看看"只掉了一个"小", 仍算单单元 -> 不是唤醒形状。

        这类形态由前端当作"疑似唤醒"送去云端复核, 最终判定看云端转写,
        不在服务端形状规则里放宽(否则"小明小明"之类全被当唤醒)。
        """
        self.assertIsNone(match_wake_shape("小看看看"))

    def test_three_char_xiaoxiao_is_fast_wake(self):
        """快速「小瑞小瑞」的云转写渲染: 3 字「小小X」(2026-10-21 实测 6/6)。

        用户把「小瑞小瑞」压到 0.5~0.7 秒时, 云 ASR 把"瑞"吞成近音, 稳定给
        小小热/小小弟/小小肉/小小鳄/小小猪/小小子。第 3 字不稳定, 所以:
          - 纯唤醒(整句 3 字)一律收 —— 这是"喊一声叫醒"的主场景;
          - 带尾巴时第 3 字必须落在听岔表里, 免得"小小年纪就…"被当唤醒。
        """
        for text in ("小小热", "小小弟", "小小肉", "小小猪", "小小子", "小小鳄"):
            self.assertEqual(match_wake_shape(text), "", text)
        self.assertEqual(match_wake_shape("小小热现在几点了"), "现在几点了")
        self.assertEqual(match_wake_shape("小小弟，现在几点了"), "现在几点了")
        self.assertIsNone(match_wake_shape("小小年纪就爱看书"))
        self.assertIsNone(match_wake_shape("小小的心愿"))

    def test_shape_returns_remainder(self):
        self.assertEqual(match_wake_shape("小度小度开灯"), "开灯")
        self.assertEqual(match_wake_shape("小瑞小瑞，帮我关灯"), "帮我关灯")
        self.assertEqual(match_wake_shape("小度小度"), "")

    def test_single_unit_is_not_wake(self):
        for text in ("小度", "小想", "小瑞", "小明"):
            self.assertIsNone(match_wake_shape(text), text)

    def test_plain_speech_is_not_wake(self):
        for text in ("帮我开灯", "今天天气怎么样", "", "小", "小小"):
            self.assertIsNone(match_wake_shape(text), text)

    def test_tail_not_swallowed(self):
        """形状命中但带尾巴时只剥前缀, 尾巴必须原样保留(不能当纯唤醒吃掉)。"""
        self.assertEqual(match_wake_shape("小丽小瑞来了"), "来了")
        self.assertEqual(match_wake_shape("小度小度，把客厅灯打开"), "把客厅灯打开")

    def test_unknown_name_pair_is_not_wake(self):
        """两个单元既不相同也不在听岔表 -> 不当唤醒词(否则"小明小刚"全被剥)。
        这种冷启动轮由服务端 wake=required 门静默丢弃, 不会进 LLM。"""
        self.assertIsNone(match_wake_shape("小明小刚来了"))

    def test_strip_prefers_shape_over_exact_list(self):
        """词表里没有"小度小度", 但剥离逻辑也要能吃下它(云 ASR 的真实转写)。"""
        rest, word = strip_wake_prefix("小度小度帮我开灯", ["小瑞小瑞", "小瑞"])
        self.assertEqual(rest, "帮我开灯")
        self.assertEqual(word, "小度小度")


class WakeGateFlowTests(unittest.TestCase):
    """Exercise normalization and the real wake-required gate, not detect injection."""

    def run_gate(self, text):
        conn = SimpleNamespace(wake_required=True, config={"wakeup_words": ["小瑞小瑞"]},
                               logger=Mock(), websocket=SimpleNamespace(send=AsyncMock()))
        handler = AsyncMock(return_value=True)
        with patch.object(intentHandler, "check_direct_exit", AsyncMock(return_value=False)), \
             patch.object(intentHandler, "checkWakeupWords", AsyncMock(return_value=False)), \
             patch.object(intentHandler, "_local_intents", return_value=[
                 ("probe", "filtered", lambda text: text, handler)]):
            result = asyncio.run(intentHandler.handle_user_intent(conn, text))
        self.assertTrue(result)
        self.assertFalse(conn.wake_required)
        return conn, handler

    def test_prefixed_transcripts_reach_intent_with_correct_tail(self):
        for text, tail in (("为小鹿小鹿帮我的忙", "帮我的忙"),
                           ("喂，小瑞小瑞，现在几点了？", "现在几点了"),
                           ("喂，小月小月，现在几点了？", "现在几点了"),
                           ("为，小月小月，现在几点了？", "现在几点了"),
                           ("谢谢，小度小度，晚安。", "晚安")):
            conn, handler = self.run_gate(text)
            handler.assert_awaited_once_with(conn, tail, tail)
            conn.websocket.send.assert_not_awaited()

    def test_negation_and_narration_are_silently_rejected(self):
        import json
        for text in ("不是小度小度", "他说小度小度", "我认识小度小度", "为啥小度小度"):
            conn, handler = self.run_gate(text)
            handler.assert_not_awaited()
            conn.websocket.send.assert_awaited_once()
            self.assertEqual(json.loads(conn.websocket.send.await_args.args[0]),
                             {"type": "silent", "reason": "nowake"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
