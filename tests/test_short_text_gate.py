#!/usr/bin/env python3
"""短转写不叫 LLM 的门禁(HA 的 no_intent_match 等价物)。

2026-10-20 板端实测: 跟说窗内说"开灯", 云 ASR 转成"海海参", 意图链没命中,
文本直接喂给 LLM —— 模型只能顺着字面编: "海参？是想吃海参吗？还是..."。
用户听到的就是"莫名其妙地答话"。

成熟做法(Home Assistant Assist / Alexa / Siri)是明确的"没听清"而不是让模型
自由发挥: HA 的 pipeline 在 STT 拿不到文字时抛 stt-no-text-recognized,
意图匹配不上时回 no_intent_match("Sorry, I didn't understand."), 两者都是
终止事件; 只有用户显式配置了 LLM conversation agent, 才会自由闲聊。
短文本里没有足够信息, 任何模型都只能编, 所以先挡在 LLM 之前。

约束: 门必须放在本地意图族**之后** —— "开灯/下一首/停下/几点了/静音"这些
两三个字的真指令是被意图接管的, 不能因为短就被挡掉。
"""
import ast
import inspect
import os
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server" / "pkg"))

from core.handle import intentHandler  # noqa: E402


class ShortTextGateTests(unittest.TestCase):
    def test_garbage_transcripts_are_too_short(self):
        for text in ("海海参", "小看", "嗯啊", "好好好"):
            self.assertTrue(intentHandler.is_too_short_to_chat(text), text)

    def test_real_sentences_pass(self):
        for text in ("今天天气怎么样", "给我讲一个笑话", "帮我开客厅的灯"):
            self.assertFalse(intentHandler.is_too_short_to_chat(text), text)

    def test_threshold_is_env_tunable(self):
        old = os.environ.get("XR_MIN_CHAT_CHARS")
        try:
            os.environ["XR_MIN_CHAT_CHARS"] = "6"
            self.assertTrue(intentHandler.is_too_short_to_chat("讲个笑话"))
            self.assertFalse(intentHandler.is_too_short_to_chat("给我讲个笑话吧"))
        finally:
            if old is None:
                os.environ.pop("XR_MIN_CHAT_CHARS", None)
            else:
                os.environ["XR_MIN_CHAT_CHARS"] = old

    def test_bad_env_falls_back_to_default(self):
        old = os.environ.get("XR_MIN_CHAT_CHARS")
        try:
            os.environ["XR_MIN_CHAT_CHARS"] = "abc"
            self.assertEqual(intentHandler.min_chat_chars(),
                             intentHandler._MIN_CHAT_CHARS_DEFAULT)
            os.environ["XR_MIN_CHAT_CHARS"] = "0"
            self.assertEqual(intentHandler.min_chat_chars(),
                             intentHandler._MIN_CHAT_CHARS_DEFAULT)
        finally:
            if old is None:
                os.environ.pop("XR_MIN_CHAT_CHARS", None)
            else:
                os.environ["XR_MIN_CHAT_CHARS"] = old

    def test_gate_runs_after_intents_and_before_llm(self):
        """守卫: 门必须在意图链之后、analyze_intent_with_llm 之前。

        顺序错了会伤真指令(短命令被"没听清"吃掉)或白挡(LLM 已经答完)。
        用 AST 查源码: 这条顺序是最容易在后续重构里丢的东西。
        """
        fn = intentHandler.handle_user_intent
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        order = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("is_too_short_to_chat", "_local_intents",
                                    "analyze_intent_with_llm"):
                    order.append((node.lineno, node.func.id))
        order.sort()
        names = [n for _, n in order]
        self.assertIn("is_too_short_to_chat", names)
        self.assertLess(names.index("_local_intents"), names.index("is_too_short_to_chat"))
        self.assertLess(names.index("is_too_short_to_chat"), names.index("analyze_intent_with_llm"))

    def test_gate_runs_before_function_call_shortcut(self):
        """守卫: 门必须早于 `if conn.intent_type == "function_call": return False`。

        板内 8001 就是 Intent: function_call —— 门放在它后面等于永不执行。
        实测漏网: "不知道"(3 字) 进了 LLM, 答出"那我来帮你找找看吧"。
        """
        fn = intentHandler.handle_user_intent
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        gate_line = None
        shortcut_line = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "is_too_short_to_chat" and gate_line is None:
                    gate_line = node.lineno
            if isinstance(node, ast.Compare):
                for operand in [node.left, *node.comparators]:
                    if isinstance(operand, ast.Constant) and operand.value == "function_call":
                        if shortcut_line is None or node.lineno < shortcut_line:
                            shortcut_line = node.lineno
        self.assertIsNotNone(gate_line, "短文本门不见了")
        self.assertIsNotNone(shortcut_line, "function_call 快捷返回不见了")
        self.assertLess(gate_line, shortcut_line,
                        "短文本门在 function_call 之后, 等于对 8001 永不生效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
