#!/usr/bin/env python3
"""Exercise the real gateway without pretending protocol audio is a speaker test."""
import argparse
import array
import json
import math
import time
from websockets.sync.client import connect
import opuslib_next


def probe(args):
    result = {"text": args.text, "music_packets": 0, "first_music_s": None,
              "abort_stop": False, "volume_after_abort": False, "pcm_rms": 0}
    with connect(args.url, additional_headers={"device-id": "music-probe"},
                 open_timeout=10, close_timeout=3) as ws:
        ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
            "features": {"mcp": False, "aec": False},
            "audio_params": {"format": "opus", "sample_rate": 16000,
                             "channels": 1, "frame_duration": 60}}))
        while True:
            data = ws.recv(timeout=15)
            if isinstance(data, str) and json.loads(data).get("type") == "hello":
                break
        # Allow tool initialization; handshake alone is not readiness evidence.
        time.sleep(2)
        ws.send(json.dumps({"type": "listen", "state": "detect", "text": args.text}))
        start = time.monotonic()
        music = False
        energy = samples = 0
        decoder = opuslib_next.Decoder(24000, 1)
        while time.monotonic()-start < args.timeout:
            data = ws.recv(timeout=max(0.1, args.timeout-(time.monotonic()-start)))
            if isinstance(data, bytes):
                if not music:
                    continue
                if result["first_music_s"] is None:
                    result["first_music_s"] = round(time.monotonic()-start, 3)
                result["music_packets"] += 1
                pcm = array.array("h", decoder.decode(data, 1440))
                energy += sum(sample*sample for sample in pcm)
                samples += len(pcm)
                if result["music_packets"] >= args.packets:
                    break
            else:
                event = json.loads(data)
                if event.get("type") == "tts" and event.get("state") == "sentence_start":
                    music = str(event.get("text", "")).startswith("正在播放，")
                if event.get("type") == "tts" and event.get("state") == "stop":
                    break
        ws.send(json.dumps({"type": "abort"}))
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            data = ws.recv(timeout=max(0.1, deadline-time.monotonic()))
            if isinstance(data, str):
                event = json.loads(data)
                if event.get("type") == "tts" and event.get("state") == "stop":
                    result["abort_stop"] = True
                    break
        ws.send(json.dumps({"type": "listen", "state": "detect", "text": "音量调到百分之五十"}))
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            data = ws.recv(timeout=max(0.1, deadline-time.monotonic()))
            if isinstance(data, str):
                event = json.loads(data)
                if event.get("type") == "volume":
                    result["volume_after_abort"] = event.get("op") == "set" and event.get("value") == 50
                    break
        result["pcm_rms"] = round(math.sqrt(energy/max(samples, 1)), 1)
    result["ok"] = (result["music_packets"] >= args.packets and
                    result["first_music_s"] is not None and result["first_music_s"] < 10 and
                    result["pcm_rms"] > 1 and result["abort_stop"] and result["volume_after_abort"])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8001/xiaorui/v1/")
    parser.add_argument("--text", default="播放两只老虎")
    parser.add_argument("--packets", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20)
    arguments = parser.parse_args()
    try:
        outcome = probe(arguments)
    except Exception as error:
        outcome = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["ok"] else 1)
