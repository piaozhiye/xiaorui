#!/usr/bin/env python3
"""Cross-gateway timer persistence against two candidate instances.

Both gateways share one XR_STATE_DIR; the probe device is dedicated. Covers:
  interop   create on leg A, list + targeted cancel on leg B, no revival
  recovery  timer set, BOTH gateways killed and restarted, exactly one delivery
"""
import os
import argparse
import json
import subprocess
import time
from websockets.sync.client import connect

DEVICE = "arch-cand-probe"


def open_leg(url):
    ws = connect(url, additional_headers={"device-id": DEVICE},
                 open_timeout=10, close_timeout=3)
    ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
        "features": {"mcp": False, "aec": False, "announce_ack": True},
        "audio_params": {"format": "opus", "sample_rate": 16000,
                         "channels": 1, "frame_duration": 60}}))
    while True:
        data = ws.recv(timeout=15)
        if isinstance(data, str) and json.loads(data).get("type") == "hello":
            return ws


def turn(ws, command, seconds=45):
    ws.send(json.dumps({"type": "listen", "state": "detect", "text": command}))
    accepted = False
    texts = []
    packets = 0
    deadline = time.monotonic() + seconds
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
                raise RuntimeError(f"stop without audio: {command}")
            return "".join(texts)
    raise TimeoutError(command)


def collect_announce(ws, seconds, accept=True, deliveries=None):
    """Drain events; optionally ACK offers; count 时间到了 deliveries."""
    texts = []
    grants = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            data = ws.recv(timeout=max(.1, deadline - time.monotonic()))
        except TimeoutError:
            continue
        if isinstance(data, bytes):
            continue
        event = json.loads(data)
        if event.get("type") == "announce" and event.get("state") == "offer" and accept:
            ws.send(json.dumps({"type": "listen", "state": "announce_ack",
                                "request_id": event["request_id"], "accepted": True}))
        if event.get("type") == "announce" and event.get("state") == "grant":
            grants += 1
        if event.get("type") == "tts" and event.get("state") == "sentence_start":
            texts.append(event.get("text", ""))
    text = "".join(texts)
    if deliveries is not None:
        deliveries.append(text.count("时间到了"))
    return grants, text


def case_interop(a_url, b_url):
    ws_a = open_leg(a_url)
    reply = turn(ws_a, "十秒后提醒我")
    assert "提醒你" in reply, reply
    ws_b = open_leg(b_url)
    listing = turn(ws_b, "有什么提醒")
    assert ("十秒" in listing or "10秒" in listing), f"leg B cannot see leg A timer: {listing}"
    cancel_reply = turn(ws_b, "取消十秒的提醒")
    assert "已取消" in cancel_reply, cancel_reply
    # Cancelled queue item must not revive: stay idle past the due moment.
    # (A connect-time welcome may play here; it is not a timer revival.)
    grants, text = collect_announce(ws_b, 18, accept=True)
    assert "时间到了" not in text, f"revived after cancel: {grants} {text}"
    final = turn(ws_b, "有什么提醒")
    assert "没有" in final, final
    ws_a.close()
    ws_b.close()
    print(f"PASS interop cross-leg create/list/cancel visible; "
          f"no revival (listing='{listing}', final='{final}')")


def case_recovery(a_url, b_url, restart_cmd):
    ws_a = open_leg(a_url)
    reply = turn(ws_a, "二十秒后提醒我")
    assert "提醒你" in reply, reply
    ws_a.close()
    subprocess.run(restart_cmd, shell=True, check=True, timeout=240)
    # Both legs reattach with ack clients; the lease must yield ONE delivery.
    ws_a2 = open_leg(a_url)
    ws_b2 = open_leg(b_url)
    deliveries = []
    import threading
    ta = threading.Thread(target=collect_announce, args=(ws_a2, 50, True, deliveries))
    tb = threading.Thread(target=collect_announce, args=(ws_b2, 50, True, deliveries))
    ta.start(); tb.start(); ta.join(); tb.join()
    total = sum(deliveries)
    assert total == 1, f"expected exactly one delivery after restart, got {total}"
    print(f"PASS recovery both gateways killed+restarted, deliveries={total}")
    ws_a2.close()
    ws_b2.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", default="ws://127.0.0.1:28001/xiaorui/v1/")
    parser.add_argument("--b", default="ws://127.0.0.1:28002/xiaorui/v1/")
    parser.add_argument("--restart", default=None)
    args = parser.parse_args()
    if args.restart is None:
        serial = os.environ.get("XZ_BOARD_SERIAL")
        if not serial:
            parser.error("缺少板卡序列号: 传 --restart 或 export XZ_BOARD_SERIAL")
        args.restart = ("adb -s " + serial +
                " shell 'for R in cand-18001 cand-18002; do "
                "kill $(cat /data/xiaorui-gateway/instances/$R/run/server.pid) 2>/dev/null; "
                "done; sleep 2; for R in cand-18001 cand-18002; do "
                "sh /data/xiaorui-gateway/start-gateway-instance.sh $R || exit 1; done'")
    case_interop(args.a, args.b)
    case_recovery(args.a, args.b, args.restart)
    print("PASS persist 2/2")
