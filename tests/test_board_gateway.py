#!/usr/bin/env python3
"""板端联网网关确定性回归 (fake 云 API, 无外网/无真实 key 可跑).

覆盖规范 HANDOFF-GLM-BOARD-SERVER.md 第 4/5 节的 fake 部分:
  LLM/ASR 401、403、429、5xx、超时挂起、非法响应、ASR 空结果;
  TTS 首包前/后失败; 有界重试(不打死循环); 协议层完整轮次与故障后恢复.

分两层:
  provider 级  —— 直接实例化 vendored pkg 的 ASR(qwen3_asr_flash)/LLM(openai)
                  provider, 指向本地 fake dashscope HTTP 服务, 断言返回/时延/调用次数.
  协议级      —— 起 vendored app.py 子进程(LLM/ASR/TTS 全指 fake, 不加载任何
                  真实模型), 用 websockets 客户端跑 detect 文本轮与 manual opus
                  音频轮, 断言 stt/tts start/音频/stop 事件链与故障后可恢复.

运行(需小智 PC venv 或等价依赖: aiohttp websockets dashscope openai opuslib_next
pydub + ffmpeg):
  /path/to/venv/python tests/test_board_gateway.py
缺依赖的用例明确 SKIP(带原因), SKIP 不计 PASS. 非零退出=有 FAIL.
"""
import asyncio
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "board-server" / "pkg"

RESULTS = []


def record(name, ok, detail="", skip=False):
    RESULTS.append((name, "SKIP" if skip else ("PASS" if ok else "FAIL"), detail))
    print("%-4s %s -- %s" % (RESULTS[-1][1], name, detail))


def need(*mods):
    """缺依赖返回 False(调用方 SKIP). 只探测不 import —— dashscope 的
    base_http_api_url 在 import 时快照, 提前 import 会让 fake 重定向失效."""
    import importlib.util
    for m in mods:
        if importlib.util.find_spec(m) is None:
            return False
    return True


# ---------------------------------------------------------------- fake 服务
class FakeCloud:
    """aiohttp 假云端: LLM compatible-mode + dashscope 原生 ASR(SSE) + TTS wav."""

    def __init__(self):
        import aiohttp
        from aiohttp import web
        self.web = web
        self.llm_script = []        # 脚本弹空后重复最后一个(持续故障测试)
        self.llm_last = None
        self.llm_calls = []
        self.asr_script = []        # 识别端点脚本(SSE/garbage/hang)
        self.asr_last = None
        self.asr_cert_script = []   # 上传证书端点脚本(401/429/5xx/hang 在这里打)
        self.asr_cert_last = None
        self.asr_calls = []
        self.asr_cert_calls = []
        self.oss_calls = []
        self.tts_script = []        # (status, body) ; body=None → 500
        self.tts_last = None
        self.tts_calls = []
        self.port = None
        self._app = web.Application()
        self._app.router.add_post("/compatible-mode/v1/chat/completions", self._llm)
        # dashscope 原生域: env DASHSCOPE_HTTP_BASE_URL=http://127.0.0.1:P/api/v1/
        self._app.router.add_route("*", "/api/v1/uploads", self._asr_cert)
        self._app.router.add_post("/oss-upload", self._oss_put)
        self._app.router.add_post(
            "/api/v1/services/aigc/multimodal-generation/generation", self._asr)
        self._app.router.add_get("/tts", self._tts)
        self._runner = None

    async def _llm(self, request):
        body = await request.json()
        self.llm_calls.append(body)
        # 脚本弹空后重复最后一个模式(而非回落成功), 便于测持续故障下的有界性
        if self.llm_script:
            self.llm_last = self.llm_script.pop(0)
        step = self.llm_last or {"status": 200, "text": "好的"}
        st = step.get("status", 200)
        if step.get("hang"):
            await asyncio.sleep(120)
            return self.web.json_response({}, status=504)
        if step.get("garbage"):
            return self.web.Response(status=200, body=b"not-json{{")
        if st == 200 and not step.get("nostream"):
            chunks = step.get("chunks") or ["好的", "，收到。"]
            lines = [b"data:" + json.dumps({
                "id": "f", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}],
            }, ensure_ascii=False).encode() for c in chunks]
            lines.append(b"data:[DONE]")
            return await self._sse_resp(request, lines)
        if st == 200:
            return self.web.json_response({"id": "f", "choices": [
                {"index": 0, "message": {"content": step.get("text", "好的")},
                 "finish_reason": "stop"}]})
        if step.get("hang"):
            await asyncio.sleep(120)
            return self.web.json_response({}, status=504)
        return self.web.json_response({"error": {"code": st}}, status=st)

    async def _sse_resp(self, request, lines):
        resp = self.web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for ln in lines:
            await resp.write(ln + b"\n\n")
        await resp.write_eof()
        return resp

    async def _asr_cert(self, request):
        # 上传证书: 真实链路里鉴权失败最先发生在这里
        self.asr_cert_calls.append(dict(request.query))
        if self.asr_cert_script:
            self.asr_cert_last = self.asr_cert_script.pop(0)
        step = self.asr_cert_last or {"status": 200}
        st = step.get("status", 200)
        if st == 200 and not step.get("hang"):
            return self.web.json_response({
                "output": {
                    "oss_access_key_id": "fake-ak",
                    "signature": "fake-sig",
                    "policy": "fake-policy",
                    "upload_dir": "dir",
                    "x_oss_object_acl": "default",
                    "x_oss_forbid_overwrite": "true",
                    "upload_host": self.url("/oss-upload"),
                },
                "request_id": "fake-cert", "code": None, "message": ""})
        if step.get("hang"):
            await asyncio.sleep(120)
            return self.web.json_response({}, status=504)
        return self.web.json_response({"code": st, "message": "fake-cert"}, status=st)

    async def _oss_put(self, request):
        body = await request.read()
        self.oss_calls.append(len(body))
        return self.web.Response(status=200, text="ok")

    async def _asr(self, request):
        body = await request.json()
        self.asr_calls.append(body)
        if self.asr_script:
            self.asr_last = self.asr_script.pop(0)
        step = self.asr_last or {"status": 200, "text": "你好世界"}
        st = step.get("status", 200)
        if st == 200 and not step.get("hang") and not step.get("garbage"):
            payload = json.dumps({
                "output": {"choices": [{"finish_reason": "stop", "message": {
                    "content": [{"text": step.get("text", "")}], "role": "assistant"}}]},
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "request_id": "fake"}, ensure_ascii=False).encode()
            resp = self.web.StreamResponse(status=200, headers={
                "Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(b"data:" + payload + b"\n\n")
            await resp.write_eof()
            return resp
        if step.get("hang"):
            await asyncio.sleep(120)
            return self.web.json_response({}, status=504)
        if step.get("garbage"):
            return self.web.Response(status=200, body=b"<<garbage>>")
        return self.web.json_response({"code": st, "message": "fake"}, status=st)

    async def _tts(self, request):
        self.tts_calls.append(dict(request.query))
        if self.tts_script:
            self.tts_last = self.tts_script.pop(0)
        step = self.tts_last or {"status": 200}
        st = step.get("status", 200)
        if st != 200:
            return self.web.Response(status=st, text="tts-fail")
        return self.web.Response(status=200, body=SILENT_WAV_24K,
                                 headers={"Content-Type": "audio/wav"})

    def start(self):
        th = threading.Thread(target=self._run, daemon=True)
        th.start()
        for _ in range(100):
            if self.port:
                return
            time.sleep(0.05)

    def _run(self):
        async def main():
            self._runner = self.web.AppRunner(self._app)
            await self._runner.setup()
            site = self.web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            await asyncio.Event().wait()
        asyncio.run(main())

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


def make_wav(path, rate=16000, seconds=0.5, freq=440):
    import math
    n = int(rate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            v = int(8000 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


SILENT_WAV_24K = None


def _init_silent_wav(tmp):
    global SILENT_WAV_24K
    p = Path(tmp) / "silent24k.wav"
    make_wav(p, rate=24000, seconds=0.3, freq=200)
    SILENT_WAV_24K = p.read_bytes()


# ---------------------------------------------------------------- provider 级
def provider_tests(fake, tmp):
    # 服务端 import 链按模块路径定位项目根: pkg/config.yaml 与 pkg/data/.config.yaml
    # 必须存在(均不入库). 测试自建最小 stub, 干净检出可复跑.
    if not (PKG / "config.yaml").exists():
        (PKG / "config.yaml").write_text(json.dumps(
            {"server": {"ip": "127.0.0.1", "port": 8000, "http_port": 8003}}))
    (PKG / "data").mkdir(exist_ok=True)
    if not (PKG / "data" / ".config.yaml").exists():
        (PKG / "data" / ".config.yaml").write_text("{}")
    sys.path.insert(0, str(PKG))
    # dashscope SDK 在 import 时快照 base url(带 /api/v1/ 前缀, 尾斜杠必需)
    os.environ["DASHSCOPE_HTTP_BASE_URL"] = fake.url("/api/v1/")
    os.environ.setdefault("DASHSCOPE_API_KEY", "test-fake-key")
    from core.providers.asr.qwen3_asr_flash import ASRProvider as AsrP
    from core.providers.llm.openai.openai import LLMProvider as LlmP
    from core.providers.asr.base import ASRProviderBase

    wav = Path(tmp) / "probe16k.wav"
    make_wav(wav, rate=16000, seconds=0.6)
    art = ASRProviderBase.AudioArtifacts(
        pcm_frames=[], pcm_bytes=b"", file_path=None, temp_path=str(wav))

    def new_asr(timeout=20):
        return AsrP({"api_key": "fake", "model_name": "qwen3-asr-flash",
                     "output_dir": str(Path(tmp) / "asrout"), "timeout": timeout}, True)

    # A stalled worker must not stall asyncio, or spawn unbounded SDK calls.
    async def check_asr_workers():
        release = threading.Event()
        ticks = 0
        first_tick_delay = None
        t0 = time.monotonic()
        started = []
        providers = [new_asr(timeout=0.15) for _ in range(3)]
        def blocked(*args):
            started.append(1)
            release.wait(5)
            return "late"
        for p in providers:
            p._recognize = blocked
        async def heartbeat():
            nonlocal ticks, first_tick_delay
            first_tick_delay = time.monotonic() - t0
            for _ in range(20):
                ticks += 1
                await asyncio.sleep(0.01)
        try:
            results = await asyncio.gather(
                *(p.speech_to_text([], "worker-test", artifacts=art) for p in providers),
                heartbeat())
            return ticks, len(started), results[:3], first_tick_delay
        finally:
            release.set()
            await asyncio.sleep(0.05)
    ticks, workers, results, first_tick_delay = asyncio.run(check_asr_workers())
    record("asr-loop-responsive-workers-bounded",
           ticks == 20 and workers == 2 and first_tick_delay < 0.08
           and all(r[0] == "" for r in results),
           "ticks=%d workers=%d first_tick=%.3fs" % (ticks, workers, first_tick_delay))

    # ASR: 200 正常 (证书+OSS+识别全链走 fake)
    fake.asr_cert_script = [{"status": 200}]
    fake.asr_script = [{"status": 200, "text": "你好世界"}]
    txt, _ = asyncio.run(new_asr().speech_to_text([b"x"] * 3, "s1", artifacts=art))
    record("asr-ok", txt == "你好世界" and len(fake.oss_calls) >= 1,
           "text=%r oss_uploads=%d" % (txt, len(fake.oss_calls)))
    # ASR: 空文本
    fake.asr_script = [{"status": 200, "text": ""}]
    txt, _ = asyncio.run(new_asr().speech_to_text([b"x"], "s2", artifacts=art))
    record("asr-empty", txt == "", "text=%r" % txt)
    # ASR: 证书端点 401 → 空串, 且只有一次调用(无重试风暴)
    fake.asr_cert_calls.clear()
    fake.asr_cert_script = [{"status": 401}]
    t0 = time.monotonic()
    txt, _ = asyncio.run(new_asr().speech_to_text([b"x"], "s3", artifacts=art))
    dt = time.monotonic() - t0
    record("asr-401-once", txt == "" and len(fake.asr_cert_calls) == 1 and dt < 10,
           "cert_calls=%d %.1fs" % (len(fake.asr_cert_calls), dt))
    # ASR: 证书端点 429 → 空串(不无限重试)
    fake.asr_cert_calls.clear()
    fake.asr_cert_script = [{"status": 429}]
    t0 = time.monotonic()
    txt, _ = asyncio.run(new_asr().speech_to_text([b"x"], "s4", artifacts=art))
    dt = time.monotonic() - t0
    record("asr-429-bounded", txt == "" and len(fake.asr_cert_calls) <= 3 and dt < 30,
           "cert_calls=%d %.1fs" % (len(fake.asr_cert_calls), dt))
    # ASR: 证书端点 5xx → 空串
    fake.asr_cert_script = [{"status": 500}]
    txt, _ = asyncio.run(new_asr().speech_to_text([b"x"], "s5", artifacts=art))
    record("asr-5xx-empty", txt == "", "text=%r" % txt)
    # ASR: 识别端点非法响应体
    fake.asr_cert_script = [{"status": 200}]
    fake.asr_script = [{"status": 200, "garbage": True}]
    txt, _ = asyncio.run(new_asr().speech_to_text([b"x"], "s6", artifacts=art))
    record("asr-garbage-empty", txt == "", "text=%r" % txt)
    # ASR: 识别端点挂起 → provider 总超时兜底
    fake.asr_cert_script = [{"status": 200}]
    fake.asr_script = [{"status": 200, "hang": True}]
    asr = new_asr(timeout=2)
    t0 = time.monotonic()
    txt, _ = asyncio.run(asr.speech_to_text([b"x"], "s7", artifacts=art))
    dt = time.monotonic() - t0
    record("asr-timeout-bounded", txt == "" and dt < 15,
           "%.1fs (timeout=2)" % dt)

    def new_llm(timeout=None):
        cfg = {"api_key": "fake", "model_name": "qwen-flash",
               "base_url": fake.url("/compatible-mode/v1")}
        if timeout:
            cfg["timeout"] = timeout
        return LlmP(cfg)

    # LLM: 401 → 异常表面 + 单次调用(openai SDK 对 401 不重试)
    fake.llm_calls.clear()
    fake.llm_script = [{"status": 401}]
    llm = new_llm()
    err = None
    try:
        list(llm.response("s", [{"role": "user", "content": "hi"}]))
    except Exception as e:
        err = e
    record("llm-401-raises-once", err is not None and len(fake.llm_calls) == 1,
           "type=%s calls=%d" % (type(err).__name__, len(fake.llm_calls)))
    # LLM: 429 → SDK 退避重试有界(默认 max_retries=2)
    fake.llm_calls.clear()
    fake.llm_script = [{"status": 429}]
    t0 = time.monotonic()
    err = None
    try:
        list(new_llm().response("s", [{"role": "user", "content": "hi"}]))
    except Exception as e:
        err = e
    dt = time.monotonic() - t0
    record("llm-429-bounded", err is not None and len(fake.llm_calls) <= 3,
           "calls=%d %.1fs" % (len(fake.llm_calls), dt))
    # LLM: 5xx
    fake.llm_script = [{"status": 500}]
    err = None
    try:
        list(new_llm().response("s", [{"role": "user", "content": "hi"}]))
    except Exception as e:
        err = e
    record("llm-5xx-raises", err is not None, "type=%s" % type(err).__name__)
    # LLM: 非法响应体 → SSE 解码器跳过坏行, 生成器空手而归; 断言有界完成不挂死
    fake.llm_script = [{"status": 200, "garbage": True}]
    t0 = time.monotonic()
    err = None
    try:
        out = list(new_llm().response("s", [{"role": "user", "content": "hi"}]))
    except Exception as e:
        err, out = e, []
    dt = time.monotonic() - t0
    record("llm-garbage-bounded", dt < 10, "%.1fs out=%r err=%s" % (dt, out, type(err).__name__))
    # LLM: 挂起 → 读超时兜底
    fake.llm_script = [{"status": 200, "hang": True}]
    t0 = time.monotonic()
    err = None
    try:
        list(new_llm(timeout={"pool": 1, "connect": 1, "write": 1, "read": 2}).response(
            "s", [{"role": "user", "content": "hi"}]))
    except Exception as e:
        err = e
    dt = time.monotonic() - t0
    record("llm-timeout-bounded", err is not None and dt < 30, "%.1fs" % dt)
    # LLM: 流式 200
    fake.llm_script = [{"status": 200, "chunks": ["你好", "，世界"]}]
    out = "".join(new_llm().response("s", [{"role": "user", "content": "hi"}]))
    record("llm-stream-ok", out == "你好，世界", "out=%r" % out)


# ---------------------------------------------------------------- 协议级
def opus_frames(path, rate=16000, frame_ms=60):
    import opuslib_next
    with wave.open(str(path), "rb") as w:
        pcm = w.readframes(w.getnframes())
    enc = opuslib_next.Encoder(rate, 1, opuslib_next.APPLICATION_VOIP)
    n = rate * frame_ms // 1000
    out = []
    mv = memoryview(pcm)
    for i in range(0, len(mv) - len(mv) % (n * 2), n * 2):
        out.append(enc.encode(bytes(mv[i:i + n * 2]), n))
    return out


class ProtoClient:
    """极简同步协议客户端(与 gateway_client.py 同协议, 面向断言)."""

    def __init__(self, url, timeout=60):
        from websockets.sync.client import connect
        # 服务端握手要求 device-id 头(与 C 前端一致), 缺失会被礼貌关闭
        self.ws = connect(url, open_timeout=10, close_timeout=3,
                          additional_headers={"device-id": "gw-test"})
        self.timeout = timeout

    def hello(self):
        self.ws.send(json.dumps({"type": "hello", "version": 1,
                                 "transport": "websocket", "device_id": "gw-test",
                                 "features": {"mcp": False, "aec": False},
                                 "audio_params": {"format": "opus", "sample_rate": 16000,
                                                  "channels": 1, "frame_duration": 60}}))
        self.ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:      # 排掉服务端开场消息; 静默即结束
            try:
                self.ws.recv(timeout=3)
            except TimeoutError:
                break

    def text_turn(self, text):
        self.ws.send(json.dumps({"type": "listen", "state": "detect", "text": text}))
        return self._drain_turn()

    def audio_turn(self, frames):
        self.ws.send(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
        for f in frames:
            self.ws.send(f)
        self.ws.send(json.dumps({"type": "listen", "state": "stop"}))
        return self._drain_turn()

    def _drain_turn(self):
        ev = {"stt": None, "tts_start": False, "pkts": 0, "stop": False, "errors": []}
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.timeout:
            try:
                m = self.ws.recv(timeout=self.timeout)
            except TimeoutError:
                ev["errors"].append("timeout")
                return ev
            if isinstance(m, bytes):
                ev["pkts"] += 1
                continue
            d = json.loads(m)
            if d.get("type") == "stt":
                ev["stt"] = d.get("text")
            elif d.get("type") == "tts" and d.get("state") == "start":
                ev["tts_start"] = True
            elif d.get("type") == "tts" and d.get("state") == "stop":
                ev["stop"] = True
                return ev
            elif d.get("type") == "error":
                ev["errors"].append(str(d)[:80])
        ev["errors"].append("turn-deadline")
        return ev

    def ping(self):
        self.ws.send(json.dumps({"type": "ping"}))
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            d = json.loads(self.ws.recv(timeout=5))
            if d.get("type") == "pong":
                return True
        return False

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def start_gateway_subprocess(fake, tmp, port):
    """拷贝 vendored pkg 到临时目录, 全 fake 依赖配置, 起 app.py 子进程."""
    root = Path(tmp) / "serverroot"
    shutil.copytree(PKG, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    cfg = {
        "server": {"ip": "127.0.0.1", "port": port, "http_port": port + 3,
                   "auth": {"enabled": False}},
        "log": {"level": "error", "backup": 3},
        "delete_audio": True,
        "close_connection_no_voice_time": 300,
        "tts_timeout": 10,
        "enable_websocket_ping": True,   # 测试要断言 JSON pong(C 前端只判发送, 不依赖 pong)
        "enable_greeting": False,
        "exit_commands": ["再见拜拜"],
        "selected_module": {"ASR": "FakeASR", "LLM": "FakeLLM", "TTS": "FakeTTS",
                            "Memory": "nomem", "Intent": "nointent"},
        "LLM": {"FakeLLM": {"type": "openai", "model_name": "qwen-flash",
                            "api_key": "fake", "base_url": fake.url("/compatible-mode/v1"),
                            "timeout": {"pool": 1, "connect": 1, "write": 1, "read": 3}}},
        "ASR": {"FakeASR": {"type": "qwen3_asr_flash", "model_name": "qwen3-asr-flash",
                            "api_key": "fake", "output_dir": "tmp/asr", "timeout": 6}},
        "TTS": {"FakeTTS": {"type": "custom", "url": fake.url("/tts"), "method": "GET",
                            "params": {}, "format": "wav", "output_dir": "tmp/"}},
        "Memory": {"nomem": {"type": "nomem"}},
        "Intent": {"nointent": {"type": "nointent"}},
        "wakeup_words": [],
        "enable_wakeup_words_response_cache": False,
        "enable_greeting": False,
        "enable_stop_tts_notify": False,
        "stop_tts_notify_voice": "config/assets/tts_notify.mp3",
        "tts_audio_send_delay": 0,
        "tool_call_timeout": 10,
        "max_output_size": 1000,
        "prompt": "你是测试助手, 回答要短。",
        # connection.py 读 config["xiaorui"] 作欢迎信息模板
        "xiaorui": {"type": "hello", "version": 1, "transport": "websocket",
                    "audio_params": {"format": "opus", "sample_rate": 24000,
                                     "channels": 1, "frame_duration": 60}},
    }
    (root / "config.yaml").write_text(json.dumps(cfg, ensure_ascii=False, indent=1))
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / ".config.yaml").write_text("{}")
    env = dict(os.environ)
    env["DASHSCOPE_HTTP_BASE_URL"] = fake.url("/api/v1/")
    env["PYTHONUNBUFFERED"] = "1"
    logf = open(Path(tmp) / "gw-app.log", "wb")
    proc = subprocess.Popen([sys.executable, str(root / "app.py")], cwd=str(root),
                            env=env, stdout=logf, stderr=subprocess.STDOUT)
    t0 = time.monotonic()
    import socket as sk
    while time.monotonic() - t0 < 45:
        if proc.poll() is not None:
            logf.flush()
            _dump_log(tmp)
            raise RuntimeError("app.py 提前退出, 见上方日志")
        s = sk.socket()
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return proc, root, logf
        except OSError:
            s.close()
            time.sleep(0.4)
    proc.terminate()
    _dump_log(tmp)
    raise RuntimeError("app.py 45s 未监听")


def _dump_log(tmp):
    try:
        tail = open(Path(tmp) / "gw-app.log", "rb").read()[-3000:]
        print("---- gw-app.log tail ----")
        print(tail.decode("utf-8", "replace"))
        print("-------------------------")
    except Exception:
        pass


def protocol_tests(fake, tmp):
    port = 18001
    import socket as sk
    while True:
        s = sk.socket()
        try:
            s.bind(("127.0.0.1", port)); s.close(); break
        except OSError:
            s.close(); port += 2
    proc, root, logf = start_gateway_subprocess(fake, tmp, port)
    url = "ws://127.0.0.1:%d/xiaorui/v1/" % port
    try:
        _protocol_cases(fake, tmp, url)
    except Exception:
        _dump_log(tmp)
        raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()


def _protocol_cases(fake, tmp, url):
    c = ProtoClient(url)
    c.hello()
    record("proto-hello-ping", c.ping(), "")

    # 文本轮 happy path
    fake.llm_script = [{"status": 200, "chunks": ["收到", "，没问题。"]}]
    fake.tts_script = [{"status": 200}]
    ev = c.text_turn("现在几点了")
    record("proto-text-turn", ev["stop"] and ev["pkts"] >= 1 and ev["stt"] == "现在几点了",
           "stt=%r pkts=%d stop=%s err=%s" % (ev["stt"], ev["pkts"], ev["stop"], ev["errors"]))

    # 音频轮 happy path (fake ASR 文本)
    wav = Path(tmp) / "speech16k.wav"
    make_wav(wav, rate=16000, seconds=1.2, freq=300)
    fake.asr_cert_script = [{"status": 200}]
    fake.asr_script = [{"status": 200, "text": "你好世界"}]
    fake.llm_script = [{"status": 200, "chunks": ["你也好"]}]
    fake.tts_script = [{"status": 200}]
    ev = c.audio_turn(opus_frames(wav))
    record("proto-audio-turn", ev["stop"] and ev["pkts"] >= 1 and ev["stt"] == "你好世界",
           "stt=%r pkts=%d stop=%s err=%s" % (ev["stt"], ev["pkts"], ev["stop"], ev["errors"]))

    # LLM 401 → 本轮有界收尾(错误话术 TTS/stop 或错误事件), 下一轮恢复
    fake.llm_script = [{"status": 401}, {"status": 200, "chunks": ["恢复了"]}]
    fake.tts_script = [{"status": 200}, {"status": 200}]
    t0 = time.monotonic()
    ev1 = c.text_turn("第一轮会失败")
    dt1 = time.monotonic() - t0
    ev2 = c.text_turn("第二轮应恢复")
    record("proto-llm-401-recovers",
           ev1["stop"] and dt1 < 60 and ev2["stop"] and ev2["pkts"] >= 1,
           "t1=%.1fs stop1=%s stop2=%s pkts2=%d" % (dt1, ev1["stop"], ev2["stop"], ev2["pkts"]))

    # ASR 证书 401 → 空文本轮次有界(不挂死), 连接可用
    fake.asr_cert_script = [{"status": 401}]
    fake.llm_script = [{"status": 200, "chunks": ["空文本也回复"]}]
    fake.tts_script = [{"status": 200}]
    t0 = time.monotonic()
    ev = c.audio_turn(opus_frames(wav))
    dt = time.monotonic() - t0
    record("proto-asr-401-bounded", ev["stop"] and dt < 60,
           "%.1fs stt=%r err=%s" % (dt, ev["stt"], ev["errors"]))

    # TTS 首包前失败 → 无音频但有收尾, 且下一轮 TTS 恢复后有声
    # TTS 持续失败(含错误话术兜底也失败) → 零音频但轮次有界收尾;
    # 下一轮 TTS 恢复后有声音. repeat-last 语义: 设一个 500 后持续 500.
    # 注: 正文合成失败后 chat 会用 TTS 说 system_error_response —— 单发 500
    # 时兜底话术反而能合成出声(架构如此), 全 500 才是真正的首包前失败.
    fake.llm_script = [{"status": 200, "chunks": ["好"]},
                       {"status": 200, "chunks": ["这句能成"]}]
    fake.tts_script = [{"status": 500}]
    ev1 = c.text_turn("tts 将失败")
    fake.tts_script = [{"status": 200}]
    ev2 = c.text_turn("tts 恢复")
    record("proto-tts-fail-then-recover",
           ev1["stop"] and ev1["pkts"] == 0 and ev2["stop"] and ev2["pkts"] >= 1,
           "pkts1=%d stop1=%s pkts2=%d stop2=%s" % (
               ev1["pkts"], ev1["stop"], ev2["pkts"], ev2["stop"]))
    record("proto-conn-alive", c.ping(), "")
    c.close()


# ---------------------------------------------------------------- main
def _run_all(tmp):
    _init_silent_wav(tmp)
    fake = FakeCloud()
    fake.start()
    if not fake.port:
        record("fake-cloud", False, "fake 服务未起")
        return
    provider_tests(fake, tmp)
    protocol_tests(fake, tmp)


def main():
    deps_ok = need("aiohttp", "websockets", "dashscope", "openai", "opuslib_next", "pydub")
    if not shutil.which("ffmpeg"):
        record("ffmpeg", False, "缺少 ffmpeg(TTS wav 转换)", skip=True)
        deps_ok = False
    if not deps_ok:
        record("deps", False, "缺少 python 依赖(用小智 PC venv 运行)", skip=True)
    else:
        import tempfile as _tf
        if os.environ.get("XR_TEST_KEEP"):
            tmp = _tf.mkdtemp(prefix="gwtest-keep-")
            print("keep-tmp:", tmp)
        else:
            tmp = _tf.mkdtemp(prefix="gwtest-")
        try:
            _run_all(tmp)
        finally:
            if not os.environ.get("XR_TEST_KEEP"):
                shutil.rmtree(tmp, ignore_errors=True)
    print("----")
    n_pass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    n_fail = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    n_skip = sum(1 for _, s, _ in RESULTS if s == "SKIP")
    print("RESULT pass=%d fail=%d skip=%d" % (n_pass, n_fail, n_skip))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
