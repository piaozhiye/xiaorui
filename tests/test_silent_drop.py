#!/usr/bin/env python3
"""静默丢弃回执单测。

服务端在"恰好只有唤醒词"和"噪声/填充词"两条路径上是静默 return True —— 
不发任何消息。前端因此在 RESPOND 状态里等满 response_timeout(默认 20s)
才收轮, 期间麦克风是聋的: 紧随其后的指令段整段丢失(声学套件实测
A2/A3 attempt1 = missing-command), 而且最后还播一声弃轮提示音, 用户
观感就是"喊了没反应 / 总是断开"。

修法: 这两条路径发一条 {"type":"silent","reason":"wake|noise"}, 前端收到
立刻回到聆听; reason=wake 时顺便开跟说窗(服务端已经认为用户在跟说)。
"""
import ast
import asyncio
import textwrap
import inspect
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server" / "pkg"))

from core.handle import intentHandler  # noqa: E402


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send(self, payload):
        if self.fail:
            raise RuntimeError("ws gone")
        self.sent.append(payload)


class FakeLogger:
    def __init__(self):
        self.lines = []

    def bind(self, **kwargs):
        return self

    def warning(self, msg):
        self.lines.append(("warning", msg))

    def info(self, msg):
        self.lines.append(("info", msg))


class FakeConn:
    def __init__(self):
        self.websocket = FakeWebSocket()
        self.logger = FakeLogger()


class SilentDropTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)

    def test_wake_reason_payload(self):
        conn = FakeConn()
        self._run(intentHandler._notify_silent_drop(conn, "wake"))
        self.assertEqual(len(conn.websocket.sent), 1)
        msg = json.loads(conn.websocket.sent[0])
        self.assertEqual(msg["type"], "silent")
        self.assertEqual(msg["reason"], "wake")

    def test_noise_reason_payload(self):
        conn = FakeConn()
        self._run(intentHandler._notify_silent_drop(conn, "noise"))
        msg = json.loads(conn.websocket.sent[0])
        self.assertEqual((msg["type"], msg["reason"]), ("silent", "noise"))

    def test_send_failure_is_fail_safe(self):
        """回执发不出去不能把整轮搞崩(客户端断线时这段也会跑)。"""
        conn = FakeConn()
        conn.websocket.fail = True
        self._run(intentHandler._notify_silent_drop(conn, "wake"))
        self.assertTrue(any(lvl == "warning" for lvl, _ in conn.logger.lines))

    def test_both_silent_paths_notify(self):
        """守卫: 噪声门与纯唤醒两条静默路径都必须发回执, 且 reason 区分开。

        用 AST 查源码而不是"跑一遍 handler"——后者要拉起 tts/intent/配置,
        代价远大于这条断言的价值, 而漏发回执正是最容易回归的点。
        """
        fn = intentHandler.handle_user_intent
        src = textwrap.dedent(inspect.getsource(fn))
        reasons = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id == "_notify_silent_drop":
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        reasons.append(arg.value)
        self.assertIn("noise", reasons)
        self.assertIn("wake", reasons)


if __name__ == "__main__":
    unittest.main(verbosity=2)
