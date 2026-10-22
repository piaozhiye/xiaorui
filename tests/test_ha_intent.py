#!/usr/bin/env python3
"""HA 设备控制意图单测: 封闭短语匹配(灯/插座双域) + stub HA 上的 handle 全路径。

重依赖(core.handle.sendAudioHandle / intentHandler)用 sys.modules 桩替,
不拉网关重链; HA REST 用本地 http.server 线程真实现 states/services 两端点,
覆盖: 正常开/关、已开/已关短路、无设备、HA 不可达降级话术、switch 域。
"""
import asyncio
import json
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.haIntent import match_light_cmd, _ha_request  # noqa: E402

POSITIVES = [
    ("开开灯", ("on", "light")), ("关关灯", ("off", "light")),
    ("开灯", ("on", "light")), ("帮我开灯", ("on", "light")),
    ("把灯打开", ("on", "light")), ("请打开灯", ("on", "light")),
    ("开一下灯。", ("on", "light")), ("麻烦把灯关掉", ("off", "light")),
    ("关灯", ("off", "light")), ("帮我把灯关了", ("off", "light")),
    ("灯打开", ("on", "light")), ("给我关一下灯", ("off", "light")),
    ("开插座", ("on", "switch")), ("打开插座", ("on", "switch")),
    ("帮我把插座打开", ("on", "switch")), ("插座通电", ("on", "switch")),
    ("给插座断电", ("off", "switch")), ("把插座关了", ("off", "switch")),
    ("关闭插座", ("off", "switch")), ("帮我关闭插座。", ("off", "switch")),
    ("关掉插座", ("off", "switch")), ("插座关掉。", ("off", "switch")),
]
NEGATIVES = [
    "开开灯的原理是什么", "别开开灯", "关关灯是什么歌", "开开插座的原理",
    "把台灯调成暖光",            # 长尾设备控制 -> 放行 LLM
    "开灯的原理是什么",
    "灯是什么时候发明的",
    "打开空调",                  # 其他域(以后扩)
    "我想听开灯的歌",
    "把灯笼挂起来",
    "插座多少钱一个",            # 含"插座"的真问题必须放行
    "哪里能买到智能插座",
    "",
    "播放音乐",
    "几点了",
]


class _StubHA(BaseHTTPRequestHandler):
    lights = {"light.test": "off", "light.indicator": "off"}
    switches = {"switch.qmi_plug_on_p_2_1": "off",
                "switch.qmi_plug_enable_buzzer_p_6_2": "off",
                "switch.qmi_plug_countdown_p_4_3": "off"}

    def _all(self):
        return [{"entity_id": k, "state": v} for k, v in self.lights.items()] + \
               [{"entity_id": k, "state": v} for k, v in self.switches.items()]

    def _send(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        if self.path == "/api/states":
            if self.headers.get("Authorization") != "Bearer tok123":
                return self._send(401, {"message": "Unauthorized"})
            return self._send(200, self._all())
        self._send(404, {"message": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path.startswith("/api/services/"):
            parts = self.path.split("/")   # ['', 'api', 'services', domain, svc]
            domain, svc = parts[3], parts[4]
            pool = self.lights if domain == "light" else self.switches
            ids = body.get("entity_id", [])
            if isinstance(ids, str):
                ids = [ids]
            for eid in ids:
                pool[eid] = "on" if svc == "turn_on" else "off"
            return self._send(200, [])
        self._send(404, {"message": "not found"})

    def log_message(self, *a):
        pass


class HAIntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _StubHA)
        cls.port = cls.srv.server_address[1]
        cls.lights = _StubHA.lights
        cls.switches = _StubHA.switches
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_match(self):
        for t, want in POSITIVES:
            self.assertEqual(match_light_cmd(t), want, t)
        for t in NEGATIVES:
            self.assertIsNone(match_light_cmd(t), t)

    def test_client_roundtrip(self):
        r = _ha_request(f"http://127.0.0.1:{self.port}", "tok123", "/api/states")
        ids = {s["entity_id"] for s in r}
        self.assertIn("light.test", ids)
        self.assertIn("switch.qmi_plug_on_p_2_1", ids)

    def test_entity_selection(self):
        async def run():
            import core.handle.haIntent as hi
            sel_sw = await hi.ha_entities(
                f"http://127.0.0.1:{self.port}", "tok123", "switch")
            self.assertEqual([s["entity_id"] for s in sel_sw], ["switch.qmi_plug_on_p_2_1"])
            sel_li = await hi.ha_entities(
                f"http://127.0.0.1:{self.port}", "tok123", "light")
            self.assertEqual([s["entity_id"] for s in sel_li], ["light.test"])
        asyncio.run(run())

    def test_handle_paths(self):
        async def run():
            import core.handle.haIntent as hi

            spoken = []

            class _TTS:
                def store_tts_text(self, *a): pass
                def tts_one_sentence(self, conn, ct, content_detail=None, **kw):
                    spoken.append(content_detail)

            class _Queue:
                def put(self, *a): pass

            _TTS.tts_text_queue = _Queue()

            class _D:
                def put(self, *a): pass

            class _L:
                def bind(self, **kw):
                    class _R:
                        def info(self, *a): pass
                        def error(self, *a): pass
                    return _R()

            class _Conn:
                logger, dialogue, tts = _L(), _D(), _TTS()

            async def fake_stt(conn, text):
                pass

            stub_audio = type("M", (), {"send_stt_message": staticmethod(fake_stt)})
            stub_speak = type("M", (), {"speak_txt": staticmethod(
                lambda conn, text: spoken.append(text))})
            saved = {k: sys.modules.get(k) for k in
                     ("core.handle.sendAudioHandle", "core.handle.intentHandler")}
            sys.modules["core.handle.sendAudioHandle"] = stub_audio
            sys.modules["core.handle.intentHandler"] = stub_speak
            try:
                import os
                os.environ["XR_HA_URL"] = f"http://127.0.0.1:{self.port}"
                os.environ["XR_HA_TOKEN"] = "tok123"

                # 灯: off->on->短路->off
                self.assertTrue(await hi.handle_light_intent(_Conn(), "开开灯", hi.match_light_cmd("开开灯")))
                self.assertEqual(self.lights["light.test"], "on")
                self.assertIn("好的，灯开了", spoken[-1])
                spoken.clear()
                await hi.handle_light_intent(_Conn(), "开灯", ("on", "light"))
                self.assertEqual(spoken[-1], "灯已经是开的")
                spoken.clear()
                await hi.handle_light_intent(_Conn(), "关关灯", hi.match_light_cmd("关关灯"))
                self.assertEqual(self.lights["light.test"], "off")
                self.assertIn("好的，灯关了", spoken[-1])

                # 插座: off->on->off + 话术
                spoken.clear()
                self.assertTrue(await hi.handle_light_intent(_Conn(), "打开插座", ("on", "switch")))
                self.assertEqual(self.switches["switch.qmi_plug_on_p_2_1"], "on")
                self.assertIn("好的，插座开了", spoken[-1])
                spoken.clear()
                await hi.handle_light_intent(_Conn(), "给插座断电", ("off", "switch"))
                self.assertEqual(self.switches["switch.qmi_plug_on_p_2_1"], "off")
                self.assertIn("好的，插座关了", spoken[-1])
                # 灯开关不误伤插座
                self.assertEqual(self.lights["light.test"], "off")

                # 无可用插座(清空)
                keep = dict(self.switches); self.switches.clear()
                spoken.clear()
                self.assertTrue(await hi.handle_light_intent(_Conn(), "开插座", ("on", "switch")))
                self.assertEqual(spoken[-1], "家里还没有能控制的插座")
                self.switches.update(keep)

                # HA 不可达: 降级话术且吃掉本轮
                os.environ["XR_HA_URL"] = "http://127.0.0.1:1"
                spoken.clear()
                self.assertTrue(await hi.handle_light_intent(_Conn(), "开灯", ("on", "light")))
                self.assertEqual(spoken[-1], "设备控制没连上，稍后再试")
                os.environ["XR_HA_URL"] = f"http://127.0.0.1:{self.port}"
            finally:
                for k, v in saved.items():
                    if v is None:
                        sys.modules.pop(k, None)
                    else:
                        sys.modules[k] = v

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
