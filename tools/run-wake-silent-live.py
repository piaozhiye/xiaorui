#!/usr/bin/env python3
"""Exercise an isolated wake/silent candidate; always restore production.

Requires --apply: briefly stops the real frontend and both gateways. Does not
promote, change mixer/model/thresholds, reboot, or use a fake gateway. Evidence
and a board-side backup survive failures. Run host gates before invoking.
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
from wake_live_acceptance import quiet_score, wake_score

ROOT = Path(__file__).resolve().parents[1]
BASE = "/data/xiaorui-gateway"
PROD = "/data/xiaorui-server/board-scripts/xiaorui_live"
LOGS = ["/tmp/xr_live.log"] + [
    f"{BASE}/instances/{r}/work/tmp/server.log" for r in (8001, 8002)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", required=True)
    ap.add_argument("--serial", default=os.environ.get("XZ_BOARD_SERIAL"))
    ap.add_argument("--quiet-seconds", type=int, default=180)
    ap.add_argument("--corpus", type=Path, help="optional manifest: cases with wav/positive")
    ap.add_argument("--ha", action="store_true")
    args = ap.parse_args()
    if not args.serial:
        ap.error("缺少板卡序列号: 传 --serial 或 export XZ_BOARD_SERIAL")
    if args.quiet_seconds < 180:
        ap.error("quiet observation must be at least 180 seconds")
    adb = ["adb", "-s", args.serial]
    stamp = str(time.time_ns())
    out = Path("/tmp/codex-wake-live-" + stamp)
    out.mkdir(mode=0o700)
    backup = BASE + "/backups/wake-silent-" + stamp
    release = BASE + "/releases/wake-silent-" + stamp + "/server"
    candidate = "/data/xiaorui-server/board-scripts/candidates/wake-" + stamp + "/xiaorui_live"
    report = dict(ok=False, restored=False, evidence=str(out), backup=backup,
                  candidate=candidate, release=release)
    changed = False
    cron = None

    def board(cmd, timeout=30):
        return subprocess.run(adb + ["shell", cmd], check=True, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()

    def remote(code, timeout=30):
        return subprocess.run(adb + ["shell", "/data/miniforge/bin/python -"],
                              input=code, check=True, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()

    def push(local, target):
        subprocess.run(adb + ["push", str(local), target], check=True,
                       capture_output=True, timeout=30)
        actual = board("sha256sum " + shlex.quote(target)).split()[0]
        if actual != hashlib.sha256(local.read_bytes()).hexdigest():
            raise RuntimeError("push hash mismatch")

    def stop_frontend():
        pids = board("pgrep -x xiaorui_live || true").split()
        for pid in pids:
            exe = board(f"readlink /proc/{pid}/exe")
            if exe not in (PROD, candidate):
                raise RuntimeError("unknown frontend: " + exe)
        for pid in pids:
            board(f"kill -TERM {int(pid)}")
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if not board("pgrep -x xiaorui_live || true"):
                return
            time.sleep(1)
        raise TimeoutError("frontend exit; no forced kill")

    def start_frontend(path):
        board("cd /data/xiaorui-server; . ./xiaorui.env; "
              "unset XR_TEXT_INJECT; "
              "(setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/"
              "sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib "
              + shlex.quote(path) + " >/tmp/xr_live.log 2>&1 </dev/null &)")
        end = time.monotonic() + 120
        while time.monotonic() < end:
            log = board("tail -40 /tmp/xr_live.log")
            if "ambient=" in log and "聆听中" in log:
                pids = board("pgrep -x xiaorui_live").split()
                if len(pids) != 1 or board(f"readlink /proc/{pids[0]}/exe") != path:
                    raise RuntimeError("frontend identity mismatch")
                return int(pids[0])
            time.sleep(2)
        raise TimeoutError("frontend readiness")

    def gateways(to_candidate):
        remote(f'''import pathlib,re,os,signal,shutil
b=pathlib.Path({BASE!r}); backup=pathlib.Path({backup!r})
for role in ('8001','8002'):
 p=b/'instances'/role/'runtime.env'
 if {to_candidate!r}:
  t=p.read_text()
  assert len(re.findall('^XR_CODE_DIR=',t,re.M))==1
  assert len(re.findall('^XR_RELEASE_ID=',t,re.M))==1
  t=re.sub(r'^XR_CODE_DIR=.*$', 'XR_CODE_DIR='+{release!r},t,flags=re.M)
  t=re.sub(r'^XR_RELEASE_ID=.*$', 'XR_RELEASE_ID='+{('wake-silent-'+stamp)!r},t,flags=re.M)
  p.write_text(t)
 else:
  shutil.copy2(backup/(role+'.env'),p)
 pid=int((p.parent/'run/server.pid').read_text())
 try:
  cmd=pathlib.Path('/proc/'+str(pid)+'/cmdline').read_bytes()
  assert b'/app.py' in cmd, 'pidfile is not gateway'
  os.kill(pid,signal.SIGTERM)
 except FileNotFoundError: pass
''')
        for role in (8001, 8002):
            board(f"sh {BASE}/start-gateway-instance.sh {role}", timeout=150)

    def offsets():
        return [int(board(f"wc -c < {p}")) for p in LOGS]

    def collect(label, before):
        values = []
        for i, (path, offset) in enumerate(zip(LOGS, before)):
            if int(board(f"wc -c < {path}")) < offset:
                raise RuntimeError("log rotated during case")
            data = board(f"tail -c +{offset + 1} {path}")
            (out / f"{label}-{i}.log").write_text(data)
            values.append(data)
        return values

    def pause(seconds):
        # Bounded slices permit interrupt/cleanup; callers print each case.
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(min(2, end - time.monotonic()))

    def wait_board_quiet(window=32, timeout=240):
        """等板子真正安静(前端日志 mtime 距今>=window 秒, 板钟口径)才放下一句.

        固定 sleep 的教训: 回答->跟说窗->屋内余音再触发一轮的链式应答可以
        超过任何拍脑袋的定长; 下一句语料压着 TTS 播出, 轮次全乱(用户实测).
        任何新日志写入(回答/噪声轮)都会重置等待, 只会更保守. 与
        run-ha-acoustic.py 的 wait_quiet 同一判据.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            age = int(board("echo $(( $(date +%s) - $(stat -c %Y /tmp/xr_live.log) ))"))
            if age >= window:
                return True
            time.sleep(min(3.0, max(0.5, window - age) + 1))
        return False

    try:
        snapshot = json.loads(remote(f'''import pathlib,json,sqlite3
b=pathlib.Path({BASE!r}); c=sqlite3.connect('file:'+str(b/'state/timers.sqlite3')+'?mode=ro',uri=True)
assert c.execute("select count(*) from timers where state in ('pending','claimed')").fetchone()[0]==0, 'active reminders'
dirs=[]
for role in ('8001','8002'):
 import re
 t=(b/'instances'/role/'runtime.env').read_text()
 dirs.append(re.search(r'^XR_CODE_DIR=(.*)$',t,re.M)[1].strip("\\\"'"))
assert dirs[0]==dirs[1]
print(json.dumps(dict(code_dir=dirs[0])))
'''))
        old = snapshot["code_dir"]
        if not old.startswith(BASE + "/releases/") or not old.endswith("/server"):
            raise RuntimeError("unexpected active release")
        if board("pgrep -x xr_cand_live || true"):
            raise RuntimeError("stray candidate frontend")
        if len(board("pgrep -x xiaorui_live").split()) != 1:
            raise RuntimeError("expected one production frontend")
        report["before"] = snapshot
        report["production_sha256"] = board("sha256sum " + PROD).split()[0]
        report["candidate_sha256"] = hashlib.sha256((ROOT / "board/xiaorui_live").read_bytes()).hexdigest()
        board(f"umask 077; mkdir -p {backup} {shlex.quote(str(Path(candidate).parent))} "
              f"{str(Path(release).parent)}; cp -p {PROD} {backup}/production; "
              f"cp -a {old} {release}; cp /tmp/xr_live.log {backup}/before.log; "
              + "; ".join(f"cp -p {BASE}/instances/{r}/runtime.env {backup}/{r}.env" for r in (8001, 8002)))
        push(ROOT / "board/xiaorui_live", candidate)
        board("chmod 755 " + candidate)
        push(ROOT / "board-server/pkg/core/handle/intentHandler.py",
             release + "/core/handle/intentHandler.py")
        cron = board("pidof crond")
        if not cron.isdigit() or board(f"awk '/^State:/ {{print $2}}' /proc/{cron}/status") == "T":
            raise RuntimeError("cron absent or already paused")
        board(f"kill -STOP {cron}")
        changed = True
        stop_frontend()
        gateways(True)
        report["candidate_pid"] = start_frontend(candidate)
        board(f"kill -CONT {cron}")
        print("candidate ready; waiting for board to settle before quiet window", flush=True)
        if not wait_board_quiet():
            print("board never went quiet before observation", flush=True)
        print("observing quiet environment for 180s", flush=True)
        before = offsets()
        pause(args.quiet_seconds)
        frontend, *server = collect("quiet", before)
        report["quiet"] = quiet_score(frontend)
        print("quiet " + json.dumps(report["quiet"]), flush=True)
        if args.corpus:
            report["wake_cases"] = []
            for item in json.loads(args.corpus.read_text())["cases"]:
                print("settle then acoustic case: " + item["id"], flush=True)
                if not wait_board_quiet():
                    print("board never went quiet before case " + item["id"], flush=True)
                pause(3)
                before = offsets()
                subprocess.run(["aplay", "-q", "-D", "plughw:1,0", item["wav"]],
                               check=True, timeout=30)
                # 等"这一轮"完整落地: 先给唤醒+应答 16s 起步, 再等前端日志
                # 静默(回答链播完收轮)后才收证据, 防证据截半截
                pause(16)
                wait_board_quiet(window=8, timeout=150)
                frontend, *server = collect(item["id"], before)
                gateway = "\n".join(server)
                result = wake_score(item, frontend, gateway)
                report["wake_cases"].append(result)
                print(json.dumps(result), flush=True)
        if args.ha:
            print("HA acoustic: one attempt per case", flush=True)
            subprocess.run(adb + ["forward", "tcp:18123", "tcp:8123"], check=True,
                           capture_output=True, timeout=10)
            run = subprocess.run(["python3", str(ROOT / "tools/run-ha-acoustic.py"),
                    "--serial", args.serial, "--attempts", "1", "--output", str(out / "ha")],
                    timeout=900)
            report["ha_exit"] = run.returncode
        report["ok"] = (report["quiet"]["ok"]
                        and all(c["ok"] for c in report.get("wake_cases", []))
                        and report.get("ha_exit", 0) == 0)
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = type(error).__name__
        print("validation error: " + type(error).__name__, flush=True)
    finally:
        if changed:
            try:
                board(f"kill -STOP {cron}")
                board(f"cp /tmp/xr_live.log {backup}/candidate.log")
                stop_frontend()
                gateways(False)
                if board("sha256sum " + PROD).split()[0] != report["production_sha256"]:
                    raise RuntimeError("production binary changed externally")
                report["restored_pid"] = start_frontend(PROD)
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
