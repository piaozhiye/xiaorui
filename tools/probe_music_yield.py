#!/usr/bin/env python3
"""协议级探针: 验证网关侧「音乐让位 + 答完续播」。

2026-10-21。背景: 音乐流占着 TTS 文本线程(play_stream 阻塞在文本线程里),
音乐期间来的回答只能排队等 —— 电台是无限流, 等于永远等不到。所以音乐流要
自己盯文本队列: 出现待播正文(TEXT/FILE)就让位, 回答播完再续播。

这个探针不依赖房间安静、不依赖板子出声: 自己当一个假设备连网关, 放电台、
数音乐包、发一条问句, 然后看让位与续播有没有发生。

用法:
  adb -s <serial> forward tcp:18101 tcp:8001
  python3 tools/probe_music_yield.py --url ws://127.0.0.1:18101/xiaorui/v1/

两个踩过的坑(照做):
  1. 必须在 hello 之后**等欢迎语真正结束**(收到 tts state=stop)再发指令 ——
     欢迎语还在播时发指令会被会话协调器 abort 掉, 队列被 clear_queues 清空,
     电台请求连同音乐一起消失。
  2. listen start 要带 mode=manual(和板端前端同形): auto 模式下 startToChat
     会打断正在播放的内容, 新一轮的消息会被当成"打断残留"丢掉。
"""
import argparse
import json
import re
import time


class YieldScore:
    """Only real packets in ordered, typed segments count as playback evidence."""
    def __init__(self, expect_text):
        self.expect_text = re.compile(expect_text)
        self.mode = None
        self.asked = False
        self.music_packets = 0
        self.answer_packets = 0
        self.resume_packets = 0
        self.answer_text = False
        self.answer_finished = False

    def observe(self, data):
        if isinstance(data, bytes):
            if not data:
                return
            if self.mode == "music":
                if not self.asked:
                    self.music_packets += 1
                elif self.answer_finished:
                    self.resume_packets += 1
            elif self.mode == "speech" and self.asked and self.answer_text:
                self.answer_packets += 1
            return
        try:
            message = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return
        if message.get("type") != "tts":
            return
        if message.get("state") == "sentence_start":
            self.mode = "music" if message.get("media") == "music" else "speech"
            if self.asked and self.mode == "speech":
                self.answer_text |= bool(self.expect_text.search(message.get("text") or ""))
        elif (message.get("state") == "stop" or
              (message.get("state") == "sentence_end" and message.get("continuation") == "music")):
            if self.mode == "speech" and self.asked and self.answer_text and self.answer_packets:
                self.answer_finished = True
            self.mode = None

    @property
    def ok(self):
        return bool(self.music_packets and self.answer_finished and self.resume_packets)

    def result(self):
        return {"ok": self.ok, "asked": self.asked,
                "music_packets": self.music_packets, "answer_packets": self.answer_packets,
                "answer_text": self.answer_text, "answer_finished": self.answer_finished,
                "resume_packets": self.resume_packets}


def send_text(ws, text):
    ws.send(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
    time.sleep(0.2)
    ws.send(json.dumps({"type": "listen", "state": "detect", "text": text}))


def wait_greeting(ws, timeout=20.0):
    """等欢迎语播完(收到 tts state=stop); 返回是否等到了。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = ws.recv(timeout=1.0)
        except TimeoutError:
            continue
        if not isinstance(data, str):
            continue
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            continue
        if message.get("type") == "tts" and message.get("state") == "stop":
            return True
    return False


def main():
    from websockets.sync.client import connect
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:18101/xiaorui/v1/")
    parser.add_argument("--radio", default="播放轻音乐电台")
    parser.add_argument("--question", default="现在几点了")
    parser.add_argument("--device", default="probe-music-yield")
    parser.add_argument("--ask-after", type=int, default=100,
                        help="收到多少个音频包后发问句(100 包 ≈ 6 秒)")
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--expect-text", default=r"现在|北京时间|时间|\d+[:：]\d+",
                        help="回答正文必须匹配的正则；换问句时同步指定")
    parser.add_argument("--no-greeting", action="store_true",
                        help="仅用于已明确关闭欢迎语的测试实例")
    args = parser.parse_args()
    if args.ask_after < 1 or args.seconds <= 0:
        parser.error("ask-after 和 seconds 必须为正数")

    events = []
    score = YieldScore(args.expect_text)
    with connect(args.url, additional_headers={"device-id": args.device},
                 open_timeout=10, close_timeout=3) as ws:
        ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                            "features": {"mcp": False, "aec": False},
                            "audio_params": {"format": "opus", "sample_rate": 16000,
                                             "channels": 1, "frame_duration": 60}}))
        while True:
            data = ws.recv(timeout=15)
            if isinstance(data, str) and json.loads(data).get("type") == "hello":
                break
        greeted = args.no_greeting or wait_greeting(ws)
        if not greeted:
            print(json.dumps({"ok": False, "reason": "greeting-timeout"}))
            return 2
        print(f"[probe] 欢迎语结束={greeted}, 发电台请求")
        time.sleep(1.0)
        send_text(ws, args.radio)

        packets = 0
        asked = False
        asked_at = None
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            try:
                data = ws.recv(timeout=1.0)
            except TimeoutError:
                continue
            score.observe(data)
            if isinstance(data, str):
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if message.get("type") == "tts":
                    kind = "tts-music" if message.get("media") == "music" else "tts-speech"
                    events.append((time.monotonic(), kind, message.get("state") or ""))
                continue
            packets += 1
            if not asked and score.music_packets >= args.ask_after:
                print(f"[probe] 音频包 {packets}, 发问句: {args.question}")
                send_text(ws, args.question)
                asked = True
                score.asked = True
                asked_at = time.monotonic()
            if score.ok:
                break
        print(f"[probe] 共收音频包 {packets} (问句后 {packets - args.ask_after})")
        for stamp, kind, detail in events:
            offset = f"{stamp - asked_at:+.2f}s" if asked_at else "  --  "
            print(f"  {offset}  {kind:11s} {detail}")
        ws.send(json.dumps({"type": "abort"}))  # Stop this probe's radio session.
    print(json.dumps(score.result(), ensure_ascii=False))
    return 0 if score.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
