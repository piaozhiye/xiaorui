#!/usr/bin/env python3
"""网关 WS 协议测试客户端 (板端网关验收用).

对指定网关跑真实协议轮次: 文本轮(detect+text)与音频轮(manual opus 整句上传),
断言 stt 回显、TTS start/音频包/stop 完整事件链, 支持多轮与 ping 存活.
每轮输出唯一编号/起止/首包时延; 结束打印机器可读 JSON, 非零退出=断言失败.

音频轮会上传语音到网关, 网关若配了云 ASR 将产生真实 API 调用 —— 必须显式
--real-api 才执行轮次场景(否则只做 hello+ping 连通冒烟). 单次调用轮数有上限.

用法:
  gateway_client.py --url ws://127.0.0.1:8001/xiaorui/v1/ --real-api \
      [--scenario text|audio|both] [--turns N] [--audio speech.wav] \
      [--json-out result.json]
"""
import argparse
import json
import struct
import sys
import time
import wave

DEFAULT_TURNS = 3
MAX_TURNS = 6          # 单次调用预算上限(真实 API 计费保护)
FIRST_AUDIO_BUDGET_S = 30.0   # 云链路首包预算(LLM 流式 + TTS 首句)
TURN_BUDGET_S = 150.0         # 单轮总预算(长回答)
TEXTS = ["给我讲一个一句话的笑话", "用两句话介绍一下你自己", "今天适合做什么运动"]

try:
    from websockets.sync.client import connect
except ImportError:
    print("缺少 websockets 库", file=sys.stderr)
    sys.exit(2)
try:
    import opuslib_next
    try:
        _APP = opuslib_next.APPLICATION_VOIP
    except AttributeError:
        _APP = 2048  # OPUS_APPLICATION_VOIP
except ImportError:
    opuslib_next = None


class Turn:
    def __init__(self, tid, kind, expect_stt=None):
        self.id = tid
        self.kind = kind            # text | audio
        self.expect_stt = expect_stt
        self.t0 = self.t1 = None
        self.stt = None
        self.tts_start = None
        self.first_audio_s = None   # detect/stop 发出 → 首个音频包
        self.audio_pkts = 0
        self.audio_bytes = 0
        self.tts_stop = None
        self.errors = []

    @property
    def ok(self):
        return (not self.errors) and self.tts_stop and self.audio_pkts >= 1

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "ok": bool(self.ok),
                "stt": self.stt, "expect_stt": self.expect_stt,
                "first_audio_s": round(self.first_audio_s, 2) if self.first_audio_s else None,
                "audio_pkts": self.audio_pkts, "audio_bytes": self.audio_bytes,
                "dur_s": round(self.t1 - self.t0, 2) if self.t1 else None,
                "errors": self.errors}


def send_json(ws, obj):
    ws.send(json.dumps(obj, ensure_ascii=False))


def run_turn(ws, turn, audio_frames=None, frame_rate_hint=None):
    """一个轮次: 发指令 → 收齐 stt/tts 事件链直到 tts stop."""
    turn.t0 = time.monotonic()
    if turn.kind == "text":
        send_json(ws, {"type": "listen", "state": "detect", "text": turn.expect_stt})
    else:
        send_json(ws, {"type": "listen", "state": "start", "mode": "manual"})
        for pkt in audio_frames:
            ws.send(pkt)
        send_json(ws, {"type": "listen", "state": "stop"})
        mark = time.monotonic()     # 音频上传完毕时刻, 首包时延以此为基准
    got_stop = False
    while not got_stop:
        deadline = turn.t0 + TURN_BUDGET_S
        remain = deadline - time.monotonic()
        if remain <= 0:
            turn.errors.append("turn-timeout %.0fs" % TURN_BUDGET_S)
            break
        try:
            msg = ws.recv(timeout=remain)
        except TimeoutError:
            turn.errors.append("recv-timeout(无 stop, 收 %d 包)" % turn.audio_pkts)
            break
        if isinstance(msg, bytes):
            turn.audio_pkts += 1
            turn.audio_bytes += len(msg)
            if turn.first_audio_s is None:
                base = mark if turn.kind == "audio" else turn.t0
                turn.first_audio_s = time.monotonic() - base
            continue
        try:
            ev = json.loads(msg)
        except ValueError:
            turn.errors.append("bad-json: %r" % msg[:80])
            continue
        et, es = ev.get("type"), ev.get("state")
        if et == "stt":
            turn.stt = ev.get("text")
            if turn.kind == "text" and turn.stt != turn.expect_stt:
                turn.errors.append("stt-echo-mismatch: %r" % turn.stt)
        elif et == "tts" and es == "start" and turn.tts_start is None:
            turn.tts_start = time.monotonic()
        elif et == "tts" and es == "stop":
            if turn.tts_start is None:
                turn.errors.append("tts-stop-without-start")
            turn.tts_stop = time.monotonic()
            got_stop = True
        elif et == "error":
            turn.errors.append("server-error: %s" % str(ev)[:120])
    turn.t1 = time.monotonic()
    if turn.tts_start is None and not turn.errors:
        turn.errors.append("no-tts-start")
    if turn.kind == "audio" and turn.stt is None and not turn.errors:
        turn.errors.append("no-stt(音频轮服务端未回识别文本)")
    if turn.first_audio_s and turn.first_audio_s > FIRST_AUDIO_BUDGET_S:
        turn.errors.append("first-audio-late %.1fs" % turn.first_audio_s)


def load_opus_frames(path, frame_ms=60, rate=16000):
    if opuslib_next is None:
        raise RuntimeError("缺少 opuslib_next")
    with wave.open(path, "rb") as w:
        assert w.getframerate() == rate, "wav 须为 %dHz" % rate
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "wav 须 16k mono s16"
        pcm = w.readframes(w.getnframes())
    enc = opuslib_next.Encoder(rate, 1, opuslib_next.APPLICATION_VOIP)
    n = rate * frame_ms // 1000
    frames, buf = [], memoryview(pcm)
    for i in range(0, len(buf) - len(buf) % (n * 2), n * 2):
        frames.append(enc.encode(bytes(buf[i:i + n * 2]), n))
    tail = bytes(buf[len(buf) - len(buf) % (n * 2):])
    if tail:
        tail += b"\x00" * (n * 2 - len(tail))     # 尾帧补零
        frames.append(enc.encode(tail, n))
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--real-api", action="store_true",
                    help="确认执行会产生真实云 API 调用的轮次场景")
    ap.add_argument("--scenario", default="both", choices=["text", "audio", "both"])
    ap.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    ap.add_argument("--audio", help="音频轮 wav (16k/mono/s16)")
    ap.add_argument("--device-id", default="gateway-client-test")
    ap.add_argument("--json-out")
    args = ap.parse_args()
    args.turns = max(1, min(args.turns, MAX_TURNS))

    report = {"url": args.url, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "real_api": args.real_api, "turns": [], "connect_ok": False, "pong": False}

    # 服务端握手要求 device-id 头(与 C 前端 backend_cloud.c 一致), 缺失会被礼貌关闭
    with connect(args.url, open_timeout=10, close_timeout=5,
                 additional_headers={"device-id": args.device_id}) as ws:
        report["connect_ok"] = True
        hello = {"type": "hello", "version": 1, "transport": "websocket",
                 "device_id": args.device_id, "features": {"mcp": False, "aec": False},
                 "audio_params": {"format": "opus", "sample_rate": 16000,
                                  "channels": 1, "frame_duration": 60}}
        send_json(ws, hello)
        # 服务端 hello 应答(含 start/detect 会话信息), 10s 内到达
        t0 = time.monotonic()
        server_hello = None
        while time.monotonic() - t0 < 10:
            msg = ws.recv(timeout=10)
            if isinstance(msg, str):
                ev = json.loads(msg)
                if ev.get("type") in ("hello", "start") or "session_id" in ev:
                    server_hello = ev
                    break
        if server_hello is None:
            report["error"] = "server hello 缺失"
            print(json.dumps(report, ensure_ascii=False))
            return 1
        send_json(ws, {"type": "listen", "state": "start", "mode": "auto"})

        if not args.real_api:
            send_json(ws, {"type": "ping"})
            t0 = time.monotonic()
            while time.monotonic() - t0 < 5:
                ev = json.loads(ws.recv(timeout=5))
                if ev.get("type") == "pong":
                    report["pong"] = True
                    break
            report["note"] = "仅连通冒烟(--real-api 才跑轮次)"
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["pong"] else 1

        turns = []
        tid = 0
        audio_frames = None
        if args.scenario in ("audio", "both"):
            if not args.audio:
                report.setdefault("skips", []).append("audio-turn: 未提供 --audio")
            else:
                audio_frames = load_opus_frames(args.audio)
        texts = TEXTS * 3
        for k in range(args.turns):
            want_audio = (args.scenario == "audio" or
                          (args.scenario == "both" and k % 2 == 1))
            if want_audio and audio_frames:
                t = Turn(tid, "audio")
                run_turn(ws, t, audio_frames)
            else:
                t = Turn(tid, "text", expect_stt=texts[k % len(texts)])
                run_turn(ws, t)
            turns.append(t)
            tid += 1
        # 轮后 ping: 连接仍可用
        send_json(ws, {"type": "ping"})
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5:
            ev = json.loads(ws.recv(timeout=5))
            if ev.get("type") == "pong":
                report["pong"] = True
                break
        report["turns"] = [t.to_dict() for t in turns]
        fa = sorted(t.first_audio_s for t in turns if t.first_audio_s is not None)
        if fa:
            report["first_audio_p50_s"] = round(fa[len(fa) // 2], 2)
            report["first_audio_p95_s"] = round(fa[min(len(fa) - 1, int(0.95 * len(fa)))], 2)
            report["first_audio_max_s"] = round(fa[-1], 2)
            report["first_audio_n"] = len(fa)

    ok = (bool(report.get("turns")) and all(t["ok"] for t in report["turns"])
          and report["pong"])
    report["verdict"] = "PASS" if ok else "FAIL"
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print("CLIENT_RESULT " + json.dumps(report, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
