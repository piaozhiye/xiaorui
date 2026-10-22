#!/usr/bin/env python3
"""Run acoustic suite on a candidate frontend; restore production on failure.

Promotion needs --promote-on-pass; no reboot, mixer, model or threshold changes.
Stopping the production frontend needs --apply (the mic is exclusive: only one
frontend can hold it, so this tool always takes the live voice assistant down).
"""
import os
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
ADB_SERIAL = os.environ.get("XZ_BOARD_SERIAL")
if not ADB_SERIAL:
    raise SystemExit("缺少板卡序列号: export XZ_BOARD_SERIAL=<序列号> 后重试")
ADB = ["adb", "-s", ADB_SERIAL]
PROD = "/data/xiaorui-server/board-scripts/xiaorui_live"
LD_LIBRARY = ("/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib"
              ":/usr/lib")
SUITE_TIMEOUT_S = 1800


def board(command, timeout=30):
    return subprocess.run(ADB + ["shell", command], capture_output=True, text=True,
                          check=True, timeout=timeout).stdout.strip()


def stop_frontend():
    pids = [p for p in board("pgrep -x xiaorui_live || true").split() if p]
    if not pids:
        raise RuntimeError("expected one frontend")
    # 麦克风被独占, 残留的第二个实例只会互相抢; 一起收掉
    for pid in pids:
        board(f"kill -TERM {pid}")
    for _ in range(20):
        alive = [p for p in pids if board(f"if kill -0 {p} 2>/dev/null; then echo alive; fi") == "alive"]
        if not alive:
            return
        time.sleep(1)
    raise TimeoutError("frontend did not exit; no blind kill")


def start_frontend(path):
    board("cd /data/xiaorui-server; . ./xiaorui.env; "
          f"setsid env LD_LIBRARY_PATH={LD_LIBRARY} "
          f"{path} >/tmp/xr_live.log 2>&1 </dev/null &")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if "mic=xr_vqe_16k 聆听中" in board("tail -40 /tmp/xr_live.log"):
            pid = board("pgrep -x xiaorui_live")
            if pid.isdigit():
                return pid
        time.sleep(2)
    raise TimeoutError("frontend did not become ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True,
                        help="explicitly allow stopping the production frontend")
    parser.add_argument("--promote-on-pass", action="store_true")
    args = parser.parse_args()
    binary = ROOT / "board/xiaorui_live"
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    tag = "ha-cadence-" + digest[:12]
    directory = f"/data/xiaorui-server/board-scripts/candidates/{tag}"
    backup = f"/data/xiaorui-gateway/backups/{tag}-{time.time_ns()}"
    report = dict(ok=False, candidate=directory + "/xiaorui_live", backup=backup)
    stopped = False
    promoted = False
    try:
        board(f"umask 077; mkdir -p {directory} {backup}; cp -p {PROD} {backup}/production; "
              f"cp /tmp/xr_live.log {backup}/before.log")
        subprocess.run(ADB + ["push", str(binary), directory + "/xiaorui_live"],
                       check=True, capture_output=True, timeout=30)
        board(f"chmod 755 {directory}/xiaorui_live")
        if board(f"sha256sum {directory}/xiaorui_live").split()[0] != digest:
            raise RuntimeError("candidate SHA mismatch")
        stop_frontend()
        stopped = True
        report["candidate_pid"] = start_frontend(directory + "/xiaorui_live")
        print("candidate ready; starting acoustic suite", flush=True)
        evidence = f"/tmp/xiaorui-ha-acoustic-{time.time_ns()}"
        run = subprocess.run(["python3", str(ROOT / "tools/run-ha-acoustic.py"),
                              "--output", evidence], timeout=SUITE_TIMEOUT_S)
        report["suite_exit"] = run.returncode
        report["evidence"] = evidence
        board(f"cp /tmp/xr_live.log {backup}/candidate.log")
        if run.returncode != 0:
            raise RuntimeError("candidate acoustic suite failed")
        if args.promote_on_pass:
            board(f"cp {directory}/xiaorui_live {PROD}.ha-new; chmod 755 {PROD}.ha-new; "
                  f"mv {PROD}.ha-new {PROD}")
            promoted_sha = board(f"sha256sum {PROD}").split()[0]
            report["promoted_sha256"] = promoted_sha
            if promoted_sha != digest:
                # 字节没对上: 生产位上现在是一个来路不明的二进制, 必须换回去
                raise RuntimeError("promotion SHA mismatch")
            promoted = True
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = type(error).__name__ + ": " + str(error)
    finally:
        if stopped and not promoted:
            try:
                if board("pgrep -x xiaorui_live || true"):
                    stop_frontend()
                # 生产位上的二进制必须回到备份那份(晋升可能只做了一半)
                board(f"cp -p {backup}/production {PROD}; chmod 755 {PROD}")
                report["restored_pid"] = start_frontend(PROD)
            except Exception as error:
                report.update(ok=False, restore_error=type(error).__name__)
                try:
                    board(f"cp -p {backup}/production {PROD}; chmod 755 {PROD}")
                    report["restored_pid"] = start_frontend(PROD)
                    report["restore_retry"] = "ok"
                except Exception as second:
                    report["restore_fatal"] = type(second).__name__
        result = Path(f"/tmp/ha-cadence-candidate-{time.time_ns()}.json")
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(dict(report, evidence=str(result)), ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
