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
GWLOG = "/data/xiaorui-gateway/data/gateway.log"


def board(command, timeout=15):
    return subprocess.run(ADB + [command], capture_output=True, text=True,
                          timeout=timeout, check=True).stdout.strip()


def offset():
    return int(board(f"wc -c <{LOG}"))


def gateway_offset():
    return int(board(f"wc -c <{GWLOG}"))


def segment(start):
    if offset() < start:
        raise RuntimeError("frontend log truncated during test")
    text = board(f"tail -c +{start + 1} {LOG}")
    # Cloud event logs may retain JSON \uXXXX escapes. Decode only those,
    # preserving already UTF-8 Chinese rather than using unicode_escape.
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)


def gateway_segment(start):
    if gateway_offset() < start:
        raise RuntimeError("gateway log truncated during test")
    return board(f"tail -c +{start + 1} {GWLOG}")


def wait(start, pattern, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = segment(start)
        if re.search(pattern, text):
            return text
        time.sleep(1)
    raise TimeoutError(f"missing {pattern!r}; recent log:\n{segment(start)[-2500:]}")


def play(device, name, backend="alsa", target=None):
    audio = str(ROOT / "tests/audio" / name)
    if backend == "pipewire":
        command = ["pw-play"]
        if target:
            command += ["--target", target]
        command.append(audio)
    elif backend == "alsa":
        command = ["aplay", "-D", device, "-q", audio]
    else:
        raise ValueError(f"unknown playback backend: {backend}")
    subprocess.run(command, check=True, timeout=90)


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


def command(device, name, expected, timeout=90, backend="alsa", target=None):
    start = offset()
    play(device, name, backend=backend, target=target)
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


def acoustic_wake_evidence(text, gateway_text):
    kws_hits = re.findall(r"KWS 命中[^:：]*: ([^\s]+)", text)
    asr_text = re.findall(r"ASR 回执: ([^\r\n]*)", text)
    gateway_asr = re.findall(r"识别文本: ([^\r\n]+)", gateway_text)
    tts = re.findall(r"语音生成成功: ([^\r\n]+)", gateway_text)
    if not kws_hits or not asr_text or not gateway_asr or not tts:
        raise RuntimeError("missing local KWS, command ASR, or gateway TTS evidence")
    return {"kws_hit": kws_hits[-1], "recognized_command": asr_text[-1],
            "gateway_asr": gateway_asr[-1], "tts": tts[-1]}


def main():
    global ADB, LOG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=["local-fault", "story", "timer", "radio-controls",
                                         "acoustic-wake"])
    parser.add_argument("--serial", default=_SER,
                        help="ADB serial; required for acoustic-wake to avoid choosing the wrong board")
    parser.add_argument("--frontend-log", default=LOG,
                        help="board frontend log path (for example /data/xiaorui/frontend.log)")
    parser.add_argument("--play-dev", default="plughw:1,0")
    parser.add_argument("--play-backend", choices=["alsa", "pipewire"], default="alsa")
    parser.add_argument("--play-target", help="optional PipeWire sink ID or name")
    args = parser.parse_args()
    if args.case == "acoustic-wake" and not args.serial:
        parser.error("acoustic-wake requires --serial (use the 4G board serial explicitly)")
    ADB = ["adb", "-s", args.serial, "shell"] if args.serial else ["adb", "shell"]
    LOG = args.frontend_log
    report = {"case": args.case, "ok": False}
    started = offset()
    gateway_started = None
    front = board("pgrep -x xiaorui_live")
    if not front.isdigit():
        raise RuntimeError("expected exactly one frontend")
    generation = board(f"cat /tmp/xr-live-health-{front}").split()[1]
    cron = None
    faulted = False
    try:
        time.sleep(32 if args.case == "acoustic-wake" else 12)
        if args.case == "acoustic-wake":
            music = board("if test -e /data/xiaorui/snapcast/music.active; then echo active; else echo idle; fi")
            if music != "idle":
                raise RuntimeError("music is active; refusing to interrupt playback for an acoustic test")
            gateway_started = gateway_offset()
            _, text = command(args.play_dev, "wake2_time.wav", "几点|时间|报时", 75,
                              backend=args.play_backend, target=args.play_target)
            report.update(acoustic_wake_evidence(text, gateway_segment(gateway_started)))
        elif args.case == "local-fault":
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
        if gateway_started is not None:
            report["gateway_log"] = gateway_segment(gateway_started)
        output = Path(f"/tmp/live-smoke-{args.case}-{time.time_ns()}.json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8', 'replace').decode('utf-8'))
        print(json.dumps({**{k: v for k, v in report.items() if k != "frontend_log"},
                          "evidence": str(output)}, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
