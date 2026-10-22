#!/usr/bin/env python3
"""B-only rollout: real-gateway negative -> immediate bare command, rollback on failure.

Does not deploy the A gateway change. Requires prior combined candidate evidence
with quiet observation and 3/3 first-attempt HA passes. No reboot or mixer writes.
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

ROOT = Path(__file__).resolve().parents[1]
PROD = "/data/xiaorui-server/board-scripts/xiaorui_live"
BASE = "/data/xiaorui-gateway"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", required=True)
    ap.add_argument("--evidence", required=True, type=Path)
    ap.add_argument("--negative-wav", required=True, type=Path)
    args = ap.parse_args()
    prior = json.loads((args.evidence / "summary.json").read_text())
    ha = json.loads((args.evidence / "ha/summary.json").read_text())
    if not (prior["restored"] and prior["ha_exit"] == 0 and prior["quiet"]["ok"]
            and ha["passed"] == 3 and ha["failed"] == 0 and ha["attempts"] == 3):
        ap.error("prior candidate/restore/first-attempt HA prerequisites failed")
    candidate = prior["candidate"]
    if not re.fullmatch(r"/data/xiaorui-server/board-scripts/candidates/wake-[0-9]+/xiaorui_live", candidate):
        ap.error("unexpected candidate path")
    stamp = str(time.time_ns())
    backup = BASE + "/backups/silent-turn-b-" + stamp
    out = Path("/tmp/silent-turn-b-" + stamp)
    out.mkdir(mode=0o700)
    adb_SERIAL = os.environ.get("XZ_BOARD_SERIAL")
    if not adb_SERIAL:
        raise SystemExit("缺少板卡序列号: export XZ_BOARD_SERIAL=<序列号> 后重试")
    adb = ["adb", "-s", adb_SERIAL]
    result = dict(ok=False, backup=backup, evidence=str(out), gateway_changed=False)
    changed = False
    cron = None

    def board(cmd, timeout=30):
        return subprocess.run(adb + ["shell", cmd], check=True, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()

    def stop():
        pids = board("pgrep -x xiaorui_live || true").split()
        if len(pids) > 1:
            raise RuntimeError("multiple frontends; refusing broad kill")
        if pids:
            pid = int(pids[0])
            if board(f"readlink /proc/{pid}/exe") not in (PROD, candidate):
                raise RuntimeError("unexpected frontend executable")
            board(f"kill -TERM {pid}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not board("pgrep -x xiaorui_live || true"):
                return
            time.sleep(1)
        raise TimeoutError("frontend exit")

    def start(path):
        board("cd /data/xiaorui-server; . ./xiaorui.env; unset XR_TEXT_INJECT; "
              "(setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/"
              "sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib "
              + shlex.quote(path) + " >/tmp/xr_live.log 2>&1 </dev/null &)")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            log = board("tail -40 /tmp/xr_live.log")
            if "聆听中" in log and "ambient=" in log:
                pid = board("pgrep -x xiaorui_live")
                if not pid.isdigit() or board(f"readlink /proc/{pid}/exe") != path:
                    raise RuntimeError("frontend identity")
                return int(pid)
            time.sleep(2)
        raise TimeoutError("frontend ready")

    def audio_case(name, wav):
        paths = ["/tmp/xr_live.log", BASE + "/instances/8001/work/tmp/server.log"]
        offsets = [int(board(f"wc -c < {p}")) for p in paths]
        subprocess.run(["aplay", "-D", "plughw:1,0", "-q", str(wav)], check=True, timeout=30)
        deadline = time.monotonic() + 12
        front, gateway = "", ""
        while time.monotonic() < deadline:
            front, gateway = [board(f"tail -c +{n+1} {p}") for n, p in zip(offsets, paths)]
            if "回执(噪声): 收轮, 不搭话" in front:
                break
            time.sleep(.5)
        (out / (name + "-frontend.log")).write_text(front)
        (out / (name + "-gateway.log")).write_text(gateway)
        ok = ("冷启动音频交服务端 ASR" in front and "回答完毕" not in front
              and "(纯唤醒, 等指令)" not in front
              and "云端转写未含唤醒词, 不答话:" in gateway
              and bool(re.search(r'"wake"\s*:\s*"required"', gateway)))
        return ok

    try:
        expected = prior["candidate_sha256"]
        if hashlib.sha256((ROOT / "board/xiaorui_live").read_bytes()).hexdigest() != expected:
            raise RuntimeError("local build changed since evidence")
        if board("sha256sum " + candidate).split()[0] != expected:
            raise RuntimeError("candidate hash changed")
        if board("sha256sum " + PROD).split()[0] != prior["production_sha256"]:
            raise RuntimeError("production changed since evidence")
        for role in (8001, 8002):
            active = board(f"sed -n 's/^XR_CODE_DIR=//p' {BASE}/instances/{role}/runtime.env")
            if active.strip("\"'") != prior["before"]["code_dir"]:
                raise RuntimeError("gateway not restored")
        if board("pgrep -x xr_cand_live || true"):
            raise RuntimeError("stray legacy candidate")
        board(f"umask 077; mkdir -p {backup}; cp -p {PROD} {backup}/production")
        cron = board("pidof crond")
        if not cron.isdigit() or board(f"awk '/^State:/ {{print $2}}' /proc/{cron}/status") == "T":
            raise RuntimeError("cron unavailable/already paused")
        board(f"kill -STOP {cron}")
        changed = True
        stop()
        result["candidate_pid"] = start(candidate)
        result["cold_negative"] = audio_case("cold-negative", args.negative_wav)
        rejected_at = time.monotonic()
        result["immediate_bare"] = audio_case("immediate-bare", ROOT / "tests/audio/cmd_time.wav")
        result["bare_elapsed_s"] = round(time.monotonic() - rejected_at, 3)
        if not (result["cold_negative"] and result["immediate_bare"] and result["bare_elapsed_s"] < 30):
            raise RuntimeError("B acoustic gate failed")
        board(f"cp /tmp/xr_live.log {backup}/candidate.log; cp {candidate} {PROD}.wake-b-new; "
              f"chmod 755 {PROD}.wake-b-new")
        if board("sha256sum " + PROD + ".wake-b-new").split()[0] != expected:
            raise RuntimeError("promotion copy mismatch")
        stop()
        board(f"mv {PROD}.wake-b-new {PROD}")
        result["production_pid"] = start(PROD)
        result["sha256"] = board("sha256sum " + PROD).split()[0]
        result["ok"] = result["sha256"] == expected
    except (Exception, KeyboardInterrupt) as error:
        result["error"] = type(error).__name__
    finally:
        if changed and not result["ok"]:
            try:
                stop()
                board(f"cp -p {backup}/production {PROD}; chmod 755 {PROD}")
                result["restored_pid"] = start(PROD)
                result["restored"] = True
            except Exception as error:
                result["restore_error"] = type(error).__name__
        if cron and cron.isdigit():
            try:
                board(f"kill -CONT {cron}")
            except Exception as error:
                result["ok"] = False
                result["cron_error"] = type(error).__name__
        (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
