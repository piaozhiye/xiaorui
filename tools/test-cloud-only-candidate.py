#!/usr/bin/env python3
"""RK3576 frontend-only capability test; always restore the original production.

Uses real gateway, PC speaker and board microphone. Does not deploy gateways,
edit production env, change mixer, reboot or promote. Requires --apply because
the production frontend must release its exclusive audio devices temporarily.
"""
import os
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
from cloud_only_acceptance import startup_settled, window_events, capture_idle

ROOT = Path(__file__).resolve().parents[1]
PROD = "/data/xiaorui-server/board-scripts/xiaorui_live"
BASE = "/data/xiaorui-gateway"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", required=True)
    ap.add_argument("--serial", default=os.environ.get("XZ_BOARD_SERIAL"))
    ap.add_argument("--play-dev", default="plughw:1,0")
    ap.add_argument("--probe-reconnect", action="store_true",
                    help="candidate-only closed loopback endpoint; no system network changes")
    args = ap.parse_args()
    if not args.serial:
        ap.error("缺少板卡序列号: 传 --serial 或 export XZ_BOARD_SERIAL")
    adb = ["adb", "-s", args.serial]
    stamp = str(time.time_ns())
    directory = "/data/xiaorui-server/board-scripts/candidates/cloud-only-" + stamp
    candidate = directory + "/xiaorui_live"
    backup = BASE + "/backups/cloud-only-" + stamp
    out = Path("/tmp/cloud-only-" + stamp)
    out.mkdir(mode=0o700)
    report = dict(ok=False, restored=False, evidence=str(out), backup=backup)
    changed = False
    cron = None

    def board(cmd, timeout=30):
        return subprocess.run(adb + ["shell", cmd], check=True, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()

    def remote(code):
        return subprocess.run(adb + ["shell", "/data/miniforge/bin/python -"],
                              input=code, check=True, capture_output=True,
                              text=True, timeout=20).stdout.strip()

    def pause(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(min(1, end - time.monotonic()))

    def stop():
        pids = board("pgrep -x xiaorui_live || true").split()
        if len(pids) > 1:
            raise RuntimeError("multiple frontends; no broad kill")
        if pids:
            pid = int(pids[0])
            if board(f"readlink /proc/{pid}/exe") not in (PROD, candidate):
                raise RuntimeError("unexpected executable")
            board(f"kill -TERM {pid}")
        for _ in range(30):
            if not board("pgrep -x xiaorui_live || true"):
                return
            pause(1)
        raise TimeoutError("frontend exit; no forced kill")

    def start(path, cloud_only, unavailable=False):
        overrides = ("XR_BACKEND=cloud XR_CLOUD_ASR=1 XR_LOCAL_ASR=off XR_CLI_REFINE=1 XR_DIAG_AUDIO=1 "
                     "XR_MUSIC_CONTROLS=1 XR_VQE_MUSIC_CONTROLS=1 "
                     f"XR_KWS_DIR={directory}/kws " if cloud_only else "")
        if unavailable:
            overrides += "XR_CLOUD_URL=ws://127.0.0.1:9/xiaorui/v1/ "
        trace = f"strace -f -e trace=openat,execve -o {directory}/trace.log " if cloud_only else ""
        board("cd /data/xiaorui-server; . ./xiaorui.env; unset XR_TEXT_INJECT; "
              "(setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/"
              "sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib "
              + overrides + trace + shlex.quote(path) + " >/tmp/xr_live.log 2>&1 </dev/null &)")
        if unavailable:
            pause(12)
            pid = board("pgrep -x xiaorui_live")
            if not pid.isdigit() or board(f"readlink /proc/{pid}/exe") != path:
                raise RuntimeError("unavailable-endpoint candidate identity")
            return int(pid)
        end = time.monotonic() + 120
        while time.monotonic() < end:
            log = board("tail -40 /tmp/xr_live.log")
            if "聆听中" in log and "ambient=" in log:
                pid = board("pgrep -x xiaorui_live")
                if not pid.isdigit() or board(f"readlink /proc/{pid}/exe") != path:
                    raise RuntimeError("frontend identity")
                return int(pid)
            pause(2)
        raise TimeoutError("frontend readiness")

    def audio_case(name, wav, answered, cold_events=None):
        ready = False
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            front = board("cat /tmp/xr_live.log")
            if cold_events is not None and window_events(front) != cold_events:
                report[name] = dict(ok=False, reason="window reopened; test audio not played")
                print(name + " " + json.dumps(report[name]), flush=True)
                return False
            if capture_idle(front):
                ready = True
                break
            pause(.5)
        if not ready:
            report[name] = dict(ok=False, reason="frontend busy; test audio not played")
            print(name + " " + json.dumps(report[name]), flush=True)
            return False
        offset = int(board("wc -c < /tmp/xr_live.log"))
        gateway = BASE + "/instances/8001/work/tmp/server.log"
        goffset = int(board(f"wc -c < {gateway}"))
        subprocess.run(["aplay", "-q", "-D", args.play_dev, str(wav)], check=True, timeout=30)
        end = time.monotonic() + 60
        front = gw = ""
        while time.monotonic() < end:
            front = board(f"tail -c +{offset+1} /tmp/xr_live.log")
            gw = board(f"tail -c +{goffset+1} {gateway}")
            if answered and "回答完毕" in front:
                break
            if not answered and "回执(噪声)" in front:
                break
            pause(1)
        (out / (name + "-frontend.log")).write_text(front)
        (out / (name + "-gateway.log")).write_text(gw)
        if answered:
            ok = ("回答完毕" in front and "播放线程收尾 ok" in front
                  and "识别文本: 现在几点了" in gw and "发送第一段语音: 现在是" in gw)
        else:
            ok = ("冷启动音频交服务端 ASR" in front and "回答完毕" not in front
                  and "云端转写未含唤醒词, 不答话:" in gw
                  and bool(re.search(r'"wake"\s*:\s*"required"', gw)))
        report[name] = dict(ok=ok)
        print(name + " " + json.dumps(report[name]), flush=True)
        return ok

    def settle_startup(label):
        print(label + ": waiting for playback drain and actual window expiry", flush=True)
        started = last_window = time.monotonic()
        count = 0
        while time.monotonic() - started < 150:
            log = board("cat /tmp/xr_live.log")
            current = window_events(log)
            if current != count:
                count = current
                last_window = time.monotonic()
            match = re.search(r'follow_window=(\d+)s', log)
            window_s = int(match[1]) if match else 30
            now = time.monotonic()
            if startup_settled(log, now - started, now - last_window, window_s):
                report[label + "_settle_s"] = round(now - started, 2)
                (out / (label + ".log")).write_text(log)
                return count
            pause(1)
        raise TimeoutError("welcome/response did not settle; no test audio played")

    try:
        report["gateways_before"] = remote(f'''import pathlib,re,json,sqlite3
b=pathlib.Path({BASE!r}); c=sqlite3.connect('file:'+str(b/'state/timers.sqlite3')+'?mode=ro',uri=True)
assert c.execute("select count(*) from timers where state in ('pending','claimed')").fetchone()[0]==0
print(json.dumps([re.search(r'^XR_CODE_DIR=(.*)$',(b/'instances'/r/'runtime.env').read_text(),re.M)[1] for r in ('8001','8002')]))
''')
        if board("pgrep -x xr_cand_live || true"):
            raise RuntimeError("legacy candidate running")
        if len(board("pgrep -x xiaorui_live").split()) != 1:
            raise RuntimeError("expected one production frontend")
        if not board("command -v strace"):
            raise RuntimeError("strace required for no-local-ASR evidence")
        report["production_sha"] = board("sha256sum " + PROD).split()[0]
        report["candidate_sha"] = hashlib.sha256((ROOT / "board/xiaorui_live").read_bytes()).hexdigest()
        report["production_rss"] = board("grep VmRSS /proc/$(pgrep -x xiaorui_live)/status")
        board(f"umask 077; mkdir -p {directory}/kws {backup}; cp -p {PROD} {backup}/production; "
              f"cp /tmp/xr_live.log {backup}/before.log; "
              f"cp /data/rknn-test/sherpa/model-kws/* {directory}/kws/")
        subprocess.run(adb + ["push", str(ROOT / "board/xiaorui_live"), candidate],
                       check=True, capture_output=True, timeout=30)
        if board("sha256sum " + candidate).split()[0] != report["candidate_sha"]:
            raise RuntimeError("candidate hash mismatch")
        board("chmod 755 " + candidate)
        cron = board("pidof crond")
        if not cron.isdigit() or board(f"awk '/^State:/ {{print $2}}' /proc/{cron}/status") == "T":
            raise RuntimeError("cron unavailable/already paused")
        board(f"kill -STOP {cron}")
        changed = True
        stop()
        pid = start(candidate, True)
        report["candidate_pid"] = pid
        board(f"kill -CONT {cron}")
        print("cloud-only candidate ready (no gateway changes)", flush=True)
        initial_events = settle_startup("startup")
        cold = audio_case("cold_wake_time", ROOT / "tests/audio/wake2_time.wav", True,
                          cold_events=initial_events)
        pause(5)  # outside echo guard, inside the true follow-up window
        follow = audio_case("follow_time", ROOT / "tests/audio/cmd_time.wav", True)
        bare_events = settle_startup("before_cold_bare")
        bare = audio_case("cold_bare_rejected", ROOT / "tests/audio/cmd_time.wav", False,
                          cold_events=bare_events)
        full = board("cat /tmp/xr_live.log")
        trace = board("cat " + directory + "/trace.log")
        (out / "candidate.log").write_text(full)
        (out / "trace.log").write_text(trace)
        report["candidate_rss"] = board(f"grep VmRSS /proc/{pid}/status")
        report["no_local_calls"] = ("local ASR disabled:" in full and "ASR 精修(" not in full
            and "offline(20s RKNN) ready" not in full
            and not re.search(r'model-20s|\.rknn|/dev/rknpu|execve\([^\n]*sherpa-onnx-offline', trace))
        report["kws_uploaded"] = "cloud-only KWS hit: server confirms wake" in full
        report["playback_controls_disabled"] = (
            "playback voice controls disabled: local ASR is off" in full
            and "播放中USB语音控制已启用" not in full)
        if args.probe_reconnect:
            print("probing candidate-only unavailable endpoint, then recovery", flush=True)
            if board("netstat -lnt 2>/dev/null | grep -E '[:.]9[[:space:]]' || true"):
                raise RuntimeError("loopback probe port unexpectedly occupied")
            board(f"kill -STOP {cron}")
            stop()
            start(candidate, True, unavailable=True)
            offline = board("cat /tmp/xr_live.log")
            offline_trace = board("cat " + directory + "/trace.log")
            (out / "unavailable.log").write_text(offline)
            (out / "unavailable-trace.log").write_text(offline_trace)
            report["unavailable_no_fallback"] = (
                "ws 失败" in offline and "ASR 精修(" not in offline
                and "local ASR disabled:" in offline
                and not re.search(r'model-20s|\.rknn|/dev/rknpu|execve\([^\n]*sherpa-onnx-offline', offline_trace))
            stop()
            report["recovered_pid"] = start(candidate, True)
            board(f"kill -CONT {cron}")
            report["recovered"] = "[xr] connected" in board("cat /tmp/xr_live.log")
        report["ok"] = all((cold, follow, bare, report["no_local_calls"],
                            report["kws_uploaded"], report["playback_controls_disabled"],
                            report.get("unavailable_no_fallback", True), report.get("recovered", True)))
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = type(error).__name__
    finally:
        if changed:
            try:
                board(f"kill -STOP {cron}")
                board(f"cp /tmp/xr_live.log {backup}/candidate.log")
                stop()
                if board("sha256sum " + PROD).split()[0] != report["production_sha"]:
                    raise RuntimeError("production binary changed externally")
                report["restored_pid"] = start(PROD, False)
                report["restored"] = True
            except Exception as error:
                report["restore_error"] = type(error).__name__
        if cron and cron.isdigit():
            try:
                board(f"kill -CONT {cron}")
            except Exception as error:
                report["cron_error"] = type(error).__name__
                report["ok"] = False
        (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["ok"] and report["restored"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
