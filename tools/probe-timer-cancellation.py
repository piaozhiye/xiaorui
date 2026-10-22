#!/usr/bin/env python3
"""Live protocol regression. Requires an empty timer list; creates/cleans test timers.

This is NOT an acoustic test. Never runs cancel-all or restarts services.
"""
import argparse
import json
import time
import datetime
from websockets.sync.client import connect

CASES = [
    ("有什么提醒", "没有正在进行的提醒", ""),
    ("今晚八点闹钟", "今晚八点", ""),
    ("明晚八点闹钟", "明晚八点", ""),
    ("取消今晚八点闹钟", "已取消今晚八点", ""),
    ("有什么提醒", "明晚八点", "今晚八点"),
    ("取消五秒的提醒", "取消范围不明确", "已取消"),
    ("有什么提醒", "明晚八点", "今晚八点"),
    ("取消明晚八点闹钟", "已取消明晚八点", ""),
    ("设一个一小时的提醒", "1小时后提醒你", ""),
    ("设一个两小时的提醒", "2小时后提醒你", ""),
    ("取消十三小时的提醒", "取消范围不明确", "已取消"),
    ("取消999秒的提醒", "取消范围不明确", "已取消"),
    ("有什么提醒", "2小时", "没有正在"),
    ("取消一小时的提醒", "已取消1小时", ""),
    ("有什么提醒", "2小时", "1小时("),
    ("取消两小时的提醒", "已取消2小时", ""),
    ("有什么提醒", "没有正在进行的提醒", ""),
]


def turn(ws, command):
    ws.send(json.dumps({"type": "listen", "state": "detect", "text": command}))
    accepted = False
    texts = []
    packets = 0
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        data = ws.recv(timeout=max(.1, deadline - time.monotonic()))
        if isinstance(data, bytes):
            if accepted:
                packets += 1
            continue
        event = json.loads(data)
        if event.get("type") == "stt" and event.get("text") == command:
            accepted = True
        if not accepted:
            continue
        if event.get("type") == "tts" and event.get("state") == "sentence_start":
            texts.append(event.get("text", ""))
        if event.get("type") == "tts" and event.get("state") == "stop":
            if not packets:
                raise RuntimeError("stop without reply audio")
            return "".join(texts)
    raise TimeoutError(command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8001/xiaorui/v1/")
    args = parser.parse_args()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    if now.hour >= 20:
        parser.error("run before 20:00 UTC+8 so tonight/tomorrow are distinct future dates")
    completed = False
    cleanup_commands = set()
    with connect(args.url, additional_headers={"device-id": "timer-cancel-regression"},
                 open_timeout=10, close_timeout=3) as ws:
        ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
            "features": {"mcp": False, "aec": False}, "audio_params": {
                "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
        while True:
            data = ws.recv(timeout=15)
            if isinstance(data, str) and json.loads(data).get("type") == "hello":
                break
        try:
            for command, required, forbidden in CASES:
                # Remember cleanup before sending: transport failure can happen
                # after the server has already accepted a create request.
                if command in ("今晚八点闹钟", "明晚八点闹钟"):
                    cleanup_commands.add("取消" + command)
                elif command.startswith("设一个"):
                    cleanup_commands.add(command.replace("设一个", "取消"))
                reply = turn(ws, command)
                ok = required in reply and (not forbidden or forbidden not in reply)
                print(json.dumps(dict(command=command, reply=reply, ok=ok), ensure_ascii=False), flush=True)
                if not ok:
                    raise RuntimeError("timer regression failed; no automatic retry")
            completed = True
        finally:
            if not completed:
                for command in sorted(cleanup_commands):
                    try:
                        turn(ws, command)
                    except Exception:
                        print("WARNING cleanup unconfirmed: " + command, flush=True)
    print(f"PASS {len(CASES)}/{len(CASES)} protocol-only")


if __name__ == "__main__":
    main()
