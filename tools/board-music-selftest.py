#!/usr/bin/env python3
"""板端音乐自检: 文本注入 -> 听音频包 -> 用网关日志判定是音乐还是语音。

跑在板子上(需要 websockets, 网关的 venv 就带):
    /data/gwvenv/bin/python board_music_selftest.py --text 播放轻音乐电台

判定依据(网关日志, 从本轮开始的位置往后读):
  "music stream first_audio="  在线直链流(电台/在线歌单)开始出声
  "电台: <名字>"                电台意图命中
  "播放本地音乐: <文件>"       本地离线库开始出声
  "music stream finished"      流结束(在线流)
  "未找到MP3音乐文件" / "未找到可播放音乐"  内容源缺失
语音回答(普通问答)也会出音频包, 但不会有上面这些标记 —— 所以
"--expect music" 只在看到音乐标记时才 PASS, "--expect speech" 反过来。
"""
import argparse
import asyncio
import json
import os
import sys
import time

import websockets


URL = "ws://127.0.0.1:8001/xiaorui/v1/"
DEFAULT_LOG = "/data/xiaorui-gateway/data/gateway.log"

MARKERS = {
    "stream_start": "music stream first_audio=",
    "stream_done": "music stream finished",
    "radio_hit": "电台: ",
    "local_play": "播放本地音乐: ",
    "no_local": "未找到MP3音乐文件",
    "no_content": "未找到可播放音乐",
    "stream_error": "音乐流播放失败",
    "yield": "music yield",
}

# 这几个标记出现, 说明这一轮真的在放音乐(而不是 LLM 回了一句闲聊)
MUSIC_MARKERS = ("stream_start", "radio_hit", "local_play")


def log_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_from(path, offset):
    try:
        with open(path, "rb") as source:
            source.seek(offset)
            return source.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


async def run(text, max_seconds, log_path):
    offset = log_size(log_path)
    packets = []
    first = None
    stop_at = None
    tts_states = []

    async with websockets.connect(
            URL,
            additional_headers={"device-id": "music-selftest",
                                "client-id": "music-selftest-c"},
            open_timeout=10, max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": 24000,
                             "channels": 1, "frame_duration": 60},
            "device_id": "music-selftest"}))
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        await ws.send(json.dumps({
            "type": "listen", "state": "detect", "text": text}))

        while time.monotonic() - t0 < max_seconds:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                if packets:
                    break          # 电台是无限流, 收不到 stop, 收到音频就算数
                continue
            except websockets.exceptions.ConnectionClosed:
                break
            if isinstance(msg, bytes):
                now = time.monotonic() - t0
                packets.append(now)
                if first is None:
                    first = now
                continue
            try:
                obj = json.loads(msg)
            except Exception:
                continue
            if obj.get("type") == "tts":
                tts_states.append(obj.get("state"))
                if obj.get("state") == "stop":
                    stop_at = time.monotonic() - t0
                    break

    return packets, first, stop_at, tts_states, read_from(log_path, offset)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="播放轻音乐电台")
    parser.add_argument("--max-seconds", type=float, default=25.0)
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--expect", choices=("music", "speech", "audio"),
                        default="music")
    args = parser.parse_args()

    packets, first, stop_at, tts_states, log_tail = asyncio.run(
        run(args.text, args.max_seconds, args.log))

    hits = {name: marker in log_tail for name, marker in MARKERS.items()}
    music_seen = any(hits[name] for name in MUSIC_MARKERS)
    print(f"[自检] 文本={args.text!r}")
    print(f"  音频包数   : {len(packets)}")
    if first is not None:
        print(f"  首包延时   : {first:.3f}s")
    if stop_at is not None:
        print(f"  stop 延时  : {stop_at:.3f}s")
    if packets:
        gaps = [b - a for a, b in zip(packets, packets[1:])]
        print(f"  音频时长   : {packets[-1]:.2f}s")
        if gaps:
            print(f"  包间隔 max : {max(gaps):.3f}s")
    print(f"  tts 状态   : {tts_states[:8]}")
    print(f"  日志标记   : " + ", ".join(
        f"{name}={'Y' if yes else 'n'}" for name, yes in hits.items()))
    print(f"  判为音乐   : " + ("是" if music_seen else "否(只有语音回答)"))

    if args.expect == "music":
        ok = music_seen and len(packets) > 0
    elif args.expect == "speech":
        ok = len(packets) > 0 and not music_seen
    else:
        ok = len(packets) > 0
    print("  判定       : " + ("PASS" if ok else "FAIL"))

    if not ok:
        print("---- 网关日志尾部 ----")
        for line in log_tail.strip().splitlines()[-25:]:
            print("  " + line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
