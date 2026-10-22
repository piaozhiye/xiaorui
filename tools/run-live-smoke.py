#!/usr/bin/env python3
"""Production frontend smoke: scoped local-leg fault + real acoustic input.

Does not replace programs, reboot, or modify production reminders. Timer case
creates one 30s reminder and lets it expire. Logs are saved even on failure.
"""
import os
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SER = os.environ.get("XZ_BOARD_SERIAL")
# 未设序列号时回退裸 adb(单设备); 维护机多设备需 export XZ_BOARD_SERIAL
ADB = ["adb", "-s", _SER, "shell"] if _SER else ["adb", "shell"]
LOG = "/tmp/xr_live.log"


def board(command, timeout=15):
    return subprocess.run(ADB + [command], capture_output=True, text=True,
                          timeout=timeout, check=True).stdout.strip()


def offset():
    return int(board(f"wc -c <{LOG}"))


def segment(start):
    if offset() < start:
        raise RuntimeError("frontend log truncated during test")
    text = board(f"tail -c +{start + 1} {LOG}")
    # Cloud event logs may retain JSON \uXXXX escapes. Decode only those,
    # preserving already UTF-8 Chinese rather than using unicode_escape.
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)


def wait(start, pattern, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = segment(start)
        if re.search(pattern, text):
            return text
        time.sleep(1)
    raise TimeoutError(f"missing {pattern!r}; recent log:\n{segment(start)[-2500:]}")


def play(device, name):
    subprocess.run(["aplay", "-D", device, "-q", str(ROOT / "tests/audio" / name)],
                   check=True, timeout=90)


def wait_local_connection(front, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sockets = set(re.findall(r"socket:\[(\d+)\]", board(f"ls -l /proc/{front}/fd")))
        rows = board(f"cat /proc/{front}/net/tcp").splitlines()[1:]
        if any(len(parts) > 9 and parts[2].endswith(":1F42") and
               parts[3] == "01" and parts[9] in sockets
               for parts in map(str.split, rows)):
            return
        time.sleep(1)
    raise TimeoutError("frontend has no ESTABLISHED connection to 8002")


def command(device, name, expected, timeout=90):
    start = offset()
    play(device, name)
    # Correlate to recognized command, not merely the wake confirmation.
    pattern = r"(?:ASR 回执:|\(?(?:唤醒|跟说中)\)? 指令:).*?(?:" + expected + ")"
    text = wait(start, pattern, timeout)
    matched = re.search(pattern, text)
    tail = text[matched.end():]
    if "回答完毕" not in tail:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = segment(start)
            if "回答完毕" in text[matched.end():]:
                return start, text
            time.sleep(1)
        raise TimeoutError("recognized command did not complete")
    return start, text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=["local-fault", "story", "timer", "radio-controls"])
    parser.add_argument("--play-dev", default="plughw:1,0")
    args = parser.parse_args()
    report = {"case": args.case, "ok": False}
    started = offset()
    front = board("pgrep -x xiaorui_live")
    if not front.isdigit():
        raise RuntimeError("expected exactly one frontend")
    generation = board(f"cat /tmp/xr-live-health-{front}").split()[1]
    cron = None
    faulted = False
    try:
        time.sleep(12)  # finish/echo cooldown must not consume the next wake
        if args.case == "local-fault":
            wait_local_connection(front)
            pid = board("cat /data/xiaorui-gateway/instances/8002/run/server.pid")
            if not pid.isdigit() or "app.py" not in board(f"tr '\\0' ' ' </proc/{pid}/cmdline"):
                raise RuntimeError("8002 PID is not the expected gateway")
            cron = board("pidof crond")
            if not cron.isdigit():
                raise RuntimeError("expected one crond")
            board(f"kill -STOP {cron}")
            faulted = True
            board(f"kill -TERM {pid}")
            wait(started, "后台本地腿已断", 30)
            _, text = command(args.play_dev, "wake2_time.wav", "几点|时间|报时", 45)
            if "audio submitted" not in text:
                raise RuntimeError("no cloud audio submission during local outage")
            board("sh /data/xiaorui-gateway/start-gateway-instance.sh 8002", timeout=100)
            wait(started, "后台本地腿重连成功", 45)
            wait_local_connection(front)
        elif args.case == "story":
            _, text = command(args.play_dev, "wake2_story.wav", "故事", 120)
            sentences = text.count('"state": "sentence_start"')
            report["sentences"] = sentences
            if sentences < 2:
                raise RuntimeError("story did not contain multiple TTS sentences")
        elif args.case == "timer":
            gateway_offsets = {role: int(board(
                f"wc -c </data/xiaorui-gateway/instances/{role}/work/tmp/server.log"))
                for role in (8001, 8002)}
            start, _ = command(args.play_dev, "wake2_timerset30w.wav", r"三十秒|30\s*(?:秒|s)", 45)
            wait(start, "主动播报已受理", 75)
            text = wait(start, "主动播报已受理[\\s\\S]*回答完毕", 90)
            if text.count("主动播报已受理") != 1:
                raise RuntimeError("duplicate reminder acceptance")
            # Delivery completion is logged just after the frontend stop.
            time.sleep(2)
            deliveries = {}
            for role, pos in gateway_offsets.items():
                tail = board(f"tail -c +{pos + 1} /data/xiaorui-gateway/instances/{role}/work/tmp/server.log")
                deliveries[str(role)] = [line for line in tail.splitlines()
                                         if "timer delivery" in line and "success=True" in line]
            report["deliveries"] = deliveries
            if sum(map(len, deliveries.values())) != 1:
                raise RuntimeError("expected exactly one successful gateway timer delivery")
        else:
            start = offset()
            play(args.play_dev, "wake2_radio.wav")
            wait(start, r"已启用\(音乐\)", 60)
            play(args.play_dev, "wake2_volfast.wav")
            wait(start, "播放中音量", 40)
            stop = offset()
            play(args.play_dev, "wake2_stopfast.wav")
            wait(stop, "语音停止播放", 40)
        if board("pgrep -x xiaorui_live") != front:
            raise RuntimeError("frontend restarted")
        if board(f"cat /tmp/xr-live-health-{front}").split()[1] != generation:
            raise RuntimeError("frontend re-executed")
        report["ok"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if args.case == "radio-controls" and not report["ok"]:
            try:
                cleanup_start = offset()
                play(args.play_dev, "wake2_stopfast.wav")
                wait(cleanup_start, "语音停止播放|回答完毕", 40)
                report["radio_cleanup"] = "stop input completed; inspect log for stop semantics"
            except Exception as error:
                report["radio_cleanup_error"] = str(error)
        if faulted:
            try:
                board("sh /data/xiaorui-gateway/start-gateway-instance.sh 8002", timeout=100)
                wait_local_connection(front)
            except Exception as error:
                report.update(ok=False, restore_error=str(error))
        if cron:
            try:
                board(f"kill -CONT {cron}")
            except Exception as error:
                report.update(ok=False, cron_error=str(error))
        report["frontend_log"] = segment(started)
        output = Path(f"/tmp/live-smoke-{args.case}-{time.time_ns()}.json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8', 'replace').decode('utf-8'))
        print(json.dumps({**{k: v for k, v in report.items() if k != "frontend_log"},
                          "evidence": str(output)}, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
