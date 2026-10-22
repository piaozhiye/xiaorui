#!/usr/bin/env python3
"""Explicitly destructive opt-in: voice reboot, boot-ID change, autostart smoke.

No synthetic main-loop stall, no direct adb reboot fallback, no program updates.
"""
import argparse
import json
from pathlib import Path
import runpy
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
helpers = runpy.run_path(str(ROOT / "tools/run-live-smoke.py"))
board, play = helpers["board"], helpers["play"]
PYTHON = "/data/miniforge/bin/python3.14"


def readiness():
    script = (
        "import json,urllib.request; "
        "print(json.dumps([json.load(urllib.request.urlopen("
        "'http://127.0.0.1:%s/health/ready'%p,timeout=2)) for p in (8004,8005)]))"
    )
    return json.loads(board(f'{PYTHON} -c "{script}"', timeout=10))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-reboot", action="store_true", required=True)
    parser.add_argument("--play-dev", default="plughw:1,0")
    args = parser.parse_args()
    report = {"ok": False, "kind": "voice-reboot-not-watchdog-stall"}
    result = Path(f"/tmp/voice-reboot-{time.time_ns()}.json")
    try:
        query = ('import sqlite3; c=sqlite3.connect('
                 '"file:/data/xiaorui-gateway/state/timers.sqlite3?mode=ro",uri=True); '
                 'print(c.execute("select count(*) from timers where state in (?,?)",'
                 '("pending","claimed")).fetchone()[0]); c.close()')
        if int(board(f"{PYTHON} -c '{query}'")) != 0:
            raise RuntimeError("pending timers: reboot not performed")
        report["before_boot"] = board("cat /proc/sys/kernel/random/boot_id")
        report["before_ready"] = readiness()
        report["before_volume"] = board("cat /data/xiaorui-server/xiaorui_volume")
        report["before_md5"] = board("md5sum /proc/$(pgrep -x xiaorui_live)/exe").split()[0]
        start = helpers["offset"]()
        print("playing explicit voice reboot command", flush=True)
        play(args.play_dev, "wake2_reboot.wav")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                boot = board("cat /proc/sys/kernel/random/boot_id", timeout=5)
                if boot and boot != report["before_boot"]:
                    report["after_boot"] = boot
                    break
                report["pre_reboot_log"] = helpers["segment"](start)[-2500:]
            except (subprocess.SubprocessError, ValueError, RuntimeError):
                pass  # expected USB/ADB disconnection while rebooting
            time.sleep(2)
        else:
            raise TimeoutError("voice command did not change boot ID; no direct reboot fallback")
        print("boot ID changed; waiting for autostart", flush=True)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                ready = readiness()
                front = board("pgrep -x xiaorui_live")
                if (front.isdigit() and all(item.get("ready") for item in ready) and
                        [item.get("role") for item in ready] == ["8001", "8002"] and
                        "mic=asr_mic 聆听中" in board("tail -80 /tmp/xr_live.log")):
                    helpers["wait_local_connection"](front, timeout=5)
                    report.update(after_ready=ready, after_frontend=front)
                    break
            except (subprocess.SubprocessError, ValueError, RuntimeError, TimeoutError):
                pass
            time.sleep(3)
        else:
            raise TimeoutError("autostart did not become ready")
        report["after_md5"] = board(f"md5sum /proc/{front}/exe").split()[0]
        report["after_volume"] = board("cat /data/xiaorui-server/xiaorui_volume")
        if report["before_md5"] != report["after_md5"]:
            raise RuntimeError("unexpected frontend binary change")
        if report["before_volume"] != report["after_volume"]:
            raise RuntimeError("volume not preserved")
        print("autostart ready; acoustic time query next", flush=True)
        time.sleep(15)
        _, report["time_query_log"] = helpers["command"](
            args.play_dev, "wake2_time.wav", "几点|时间|报时", timeout=60)
        report["cron"] = board("pidof crond")
        if not report["cron"].isdigit():
            raise RuntimeError("crond did not autostart")
        report["ok"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8', 'replace').decode('utf-8'))
        print(json.dumps({"ok": report["ok"], "error": report.get("error"),
                          "evidence": str(result)}, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
