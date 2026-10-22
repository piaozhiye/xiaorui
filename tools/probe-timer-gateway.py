#!/usr/bin/env python3
"""Timer/alarm intent probe: send text commands over the real ws protocol,
print spoken replies. Exercises intentHandler -> timerIntent on a live gateway."""
import json
import sys
import time
from websockets.sync.client import connect

CMDS = sys.argv[1:]
URL = "ws://127.0.0.1:8001/xiaorui/v1/"

with connect(URL, additional_headers={"device-id": "timer-probe"},
             open_timeout=10, close_timeout=3) as ws:
    ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                        "features": {"mcp": False, "aec": False},
                        "audio_params": {"format": "opus", "sample_rate": 16000,
                                         "channels": 1, "frame_duration": 60}}))
    while True:
        data = ws.recv(timeout=10)
        if isinstance(data, str) and json.loads(data).get("type") == "hello":
            break
    time.sleep(1)
    for cmd in CMDS:
        ws.send(json.dumps({"type": "listen", "state": "detect", "text": cmd}))
        texts = []
        deadline = time.monotonic() + 15
        stopped = False
        while time.monotonic() < deadline and not stopped:
            try:
                data = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:
                break
            if isinstance(data, str):
                ev = json.loads(data)
                if ev.get("type") == "tts" and ev.get("state") == "sentence_start":
                    texts.append(str(ev.get("text", "")))
                if ev.get("type") == "tts" and ev.get("state") == "stop":
                    stopped = True
        print(f"CMD {cmd!r} -> {' / '.join(texts) if texts else '(no reply)'}",
              flush=True)
        time.sleep(0.5)
