#!/usr/bin/env python3
"""Three acoustic HA cases, correlated evidence, strict exit status, state restore.

Deploy the matching core/ha_audit.py + HA/sendAudio handlers to BOTH gateways
before using this suite. Old gateway logs intentionally cannot produce PASS.

两个硬前提(缺了就会出现"假通过"或"假失败",2026-10-20 两种都真出现过):

1. 每个用例先强制灯处于**相反的前置态**再播音频。目标态已等于期望态时,
   意图会走"已经是开的"短路, 零翻转 —— 不强制就会把没翻的轮次算成通过
   (实测 A1 就是 noop 假通过)。
2. 用例之间必须等**跟说窗过期**。板子应答后 30s 内免唤醒, 窗内的唤醒词会被
   当成"跟说"直接送服务端 ASR, 指令段还会被播放冷却期吃掉 —— 于是既没有
   唤醒证据也没有指令 (实测 A3 因此判 wrong-command)。

声学抖动(唤醒漏检/识别错)允许重放同一段音频, 但审计契约被破坏
(malformed/invalid/mismatched)一律立刻判失败, 不靠重放掩盖。
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

from ha_acceptance import (score, exit_code, playback_ready, parse_follow_window,
                           retryable, CASES, SETTLED)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_LOG = "/tmp/xr_live.log"
# 必须 tail loguru 的**文件 sink**: run/server.log 是进程 stdout 的副本,
# Python 块缓冲, 审计记录会晚好几条才落进去(实测 run/ 少 4 条 completed),
# 于是 completed 迟到被判成"音频没播完"。
GATEWAY_LOGS = ("/data/xiaorui-gateway/instances/8001/work/tmp/server.log",
                "/data/xiaorui-gateway/instances/8002/work/tmp/server.log")
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
# 这些理由再等也不会变好(音频已经播完, 不会再来一次), 立刻进入下一次重放。
# 反过来: 审计/音频完成/灯态都是"还没到"而不是"坏了" —— 云端首包 2.5~3.2s,
# 审计的 completed 记录在 TTS 播完后才落盘(实测比 accepted 晚 2s),
# 早收工会把"再等两秒就好"判成失败。定义见 ha_acceptance.SETTLED。

SERIAL = os.environ.get("XZ_BOARD_SERIAL")


def board(command, timeout=15):
    # 序列号校验放在首次板端操作边界: 预检类失败(如 token 缺失)仍走
    # 常规报告路径, 不被序列号错误抢先(test_shell_entrypoint 契约)。
    if not SERIAL:
        raise SystemExit("缺少板卡序列号: 传 --serial 或 export XZ_BOARD_SERIAL")
    return subprocess.run(["adb", "-s", SERIAL, "shell", command],
                          capture_output=True, text=True, timeout=timeout).stdout.strip()


def card_of(device):
    match = re.fullmatch(r"(?:plughw|hw):(\d+),\d+", device)
    return int(match.group(1)) if match else None


def read_token(path, workdir):
    """取长效 token; 源文件若全局可读, 复制一份 0600 再用, 不动用户的原件。"""
    raw = path.read_bytes()
    if path.stat().st_mode & 0o077:
        private = workdir / "token-0600.json"
        private.write_bytes(raw)
        private.chmod(0o600)
        return json.loads(raw)["ha_long_lived_token"], True
    return json.loads(raw)["ha_long_lived_token"], False


def main():
    global SERIAL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=os.environ.get("XZ_BOARD_SERIAL"))
    parser.add_argument("--play-dev", default=os.environ.get("PLAY_DEV", "plughw:1,0"))
    parser.add_argument("--ha-url", default="http://127.0.0.1:18123")
    parser.add_argument("--token-file", type=Path, default=Path("/tmp/ha_llt.json"))
    parser.add_argument("--entity", default="light.xiaoruitestlight")
    parser.add_argument("--attempts", type=int, default=2,
                        help="每个用例最多重放几次(声学抖动可重试)")
    parser.add_argument("--allow-remote", action="store_true",
                        help="允许把 token 发往非回环地址")
    parser.add_argument("--output", type=Path,
                        default=Path(f"/tmp/xiaorui-ha-acoustic-{time.time_ns()}"))
    args = parser.parse_args()
    SERIAL = args.serial
    if not re.fullmatch(r"light\.[a-z0-9_]+", args.entity):
        parser.error("entity must be a light entity ID")
    host = args.ha_url.split("//")[-1].split(":")[0].split("/")[0]
    if host not in LOOPBACK and not args.allow_remote:
        parser.error("refusing to send the token to a non-loopback host "
                     "(use --allow-remote if that is really intended)")
    try:
        args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError:
        parser.error(f"output dir exists: {args.output}")
    report = dict(passed=0, failed=0, cases=[], restored=False, attempts=0)
    initial = None
    touched = False
    token = None
    state_now = "unknown"

    def api(path, data=None):
        request = urllib.request.Request(args.ha_url.rstrip("/") + path,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={"Authorization": "Bearer " + token,
                             "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def state():
        value = api("/api/states/" + args.entity)["state"]
        if value not in ("on", "off"):
            raise RuntimeError("test light unavailable/unknown")
        return value

    def force(want):
        api("/api/services/light/turn_" + want, {"entity_id": args.entity})
        for _ in range(20):
            if state() == want:
                return True
            time.sleep(.5)
        return False

    logs = [FRONTEND_LOG] + list(GATEWAY_LOGS)

    def tail_since(offsets):
        parts = []
        for path, offset in zip(logs, offsets):
            if int(board(f"wc -c <{path}")) < offset:
                raise RuntimeError("log rotated/truncated during case")
            parts.append(board(f"tail -c +{offset + 1} {path}"))
        return parts[0], "\n".join(parts[1:])

    def wait_quiet(window):
        """等板子彻底安静再开下一轮。

        板子应答后 N 秒内免唤醒 —— 窗内的唤醒词会被当"跟说"直接送服务端 ASR,
        指令段还会被播放冷却期吃掉(实测第一例因此完全没有唤醒证据)。
        上一轮没答完时还有一轮在途, 它的动作会晚到, 把刚强制好的前置态翻回去
        (实测第二轮审计报 noop)。所以每轮开头都要等。

        判据用前端日志的 mtime: 最后一次写入距今超过 window 即视为窗口已过期。
        任何新写入(噪声轮、应答)都只会让它更保守, 不会误判。
        时差在板上算(板钟是 UTC, 宿钟是 UTC+8, 跨机相减会差 8 小时)。
        """
        deadline = time.monotonic() + window + 10
        while time.monotonic() < deadline:
            age = int(board("echo $(( $(date +%s) - $(stat -c %Y /tmp/xr_live.log) ))"))
            if age >= window:
                return True
            time.sleep(min(2.0, max(0.5, window - age)))
        return False

    try:
        token, private = read_token(args.token_file, args.output)
        report["token_source_private"] = private
        card = card_of(args.play_dev)
        if card is None:
            raise RuntimeError("cannot verify playback device: " + args.play_dev)
        report["playback"] = {}
        for control in ("Master", "Speaker"):
            result = subprocess.run(["amixer", "-c", str(card), "sget", control],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                continue
            report["playback"][control] = result.stdout
            if not playback_ready(result.stdout):
                raise RuntimeError("PC playback muted/zero: " + control)
        if not report["playback"]:
            raise RuntimeError("no playback control to verify on card %d" % card)
        report["playback_device"] = args.play_dev
        # 两个网关必须同一个 release 且都带审计模块: 只装一半的板子也能凑出 3/3
        releases = []
        for role in (8001, 8002):
            env = board(f"cat /data/xiaorui-gateway/instances/{role}/runtime.env")
            match = re.search(r"^XR_RELEASE_ID=(.*)$", env, re.M)
            releases.append(match.group(1).strip() if match else "")
        if not releases[0] or releases[0] != releases[1]:
            raise RuntimeError("gateways on different releases: " + repr(releases))
        if board(f"test -f /data/xiaorui-gateway/releases/{releases[0]}"
                 f"/server/core/ha_audit.py && echo yes") != "yes":
            raise RuntimeError("audit module missing on the board: " + releases[0])
        report["release"] = releases[0]
        report["mic"] = board("grep -h 'XR_MIC=' /data/xiaorui-server/xiaorui.env | tail -1")
        follow_window = parse_follow_window(
            board("grep -o 'follow_window=[0-9]*s' /tmp/xr_live.log | tail -1"))
        report["follow_window_s"] = follow_window
        initial = state()
        # The current HA intent controls all eligible lights, not one entity.
        # Refuse to run against a home with other controllable lights.
        eligible = {s["entity_id"] for s in api("/api/states")
                    if s.get("entity_id", "").startswith("light.") and
                    s.get("state") not in ("unavailable", "unknown") and
                    "indicator" not in s["entity_id"] and
                    "指示灯" not in s.get("attributes", {}).get("friendly_name", "")}
        if eligible != {args.entity}:
            raise RuntimeError("isolate the HA light domain before acoustic tests")
        for label, wav, want, pre in CASES:
            item = dict(label=label, want=want, ok=False, tries=[])
            for attempt in range(1, max(1, args.attempts) + 1):
                frontend, gateways = "", ""
                ok = False
                reason = "not-run"
                try:
                    touched = True
                    # 先等板子安静(跟说窗过期 + 没有在途的一轮), 再强制前置态:
                    # 顺序反了的话, 上一轮晚到的动作会把刚设好的前置态翻回去
                    wait_quiet(follow_window)
                    if not force(pre):
                        reason = "prestate-unavailable"
                    else:
                        before = state()
                        if before != pre:
                            reason = "prestate-mismatch"
                        else:
                            offsets = [int(board(f"wc -c <{path}")) for path in logs]
                            subprocess.run(["aplay", "-D", args.play_dev, "-q",
                                            str(ROOT / "tests/audio" / wav)],
                                           check=True, timeout=60)
                            deadline = time.monotonic() + 90
                            while time.monotonic() < deadline:
                                frontend, gateways = tail_since(offsets)
                                after = state()
                                ok, reason = score(frontend, gateways, want, before,
                                                   after, args.entity)
                                if ok or reason in SETTLED:
                                    break
                                time.sleep(2)
                            report["attempts"] += 1
                except Exception as error:
                    # Avoid printing URLs/headers/body that can carry private tokens.
                    ok, reason = False, type(error).__name__
                item["tries"].append(dict(attempt=attempt, reason=reason, ok=ok))
                (args.output / f"{label}-attempt{attempt}-frontend.log").write_text(frontend)
                (args.output / f"{label}-attempt{attempt}-audit.log").write_text("\n".join(
                    line for line in gateways.splitlines() if "HA_AUDIT " in line))
                if ok:
                    item["ok"] = True
                    break
                if not retryable(reason):
                    break
            report["cases"].append(item)
            report["passed" if item["ok"] else "failed"] += 1
            print(json.dumps(item, ensure_ascii=False), flush=True)
    except (Exception, KeyboardInterrupt) as error:
        report.update(error=str(error) if type(error) is RuntimeError else type(error).__name__,
                      failed=report["failed"] + 1)
    finally:
        if initial is not None:
            try:
                # 无条件复位: 读状态失败不该把灯留在亮着(turn_<state> 是幂等的)
                if touched:
                    api("/api/services/light/turn_" + initial, {"entity_id": args.entity})
                for _ in range(10):
                    try:
                        state_now = state()
                    except Exception:
                        state_now = "unknown"
                    if state_now == initial:
                        break
                    time.sleep(.5)
                report["restored"] = state_now == initial
            except Exception as error:
                report["restore_error"] = type(error).__name__
        (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"ACOUSTIC pass={report['passed']} fail={report['failed']} "
              f"restored={report['restored']} attempts={report['attempts']} "
              f"evidence={args.output}", flush=True)
    return exit_code(report["passed"], report["failed"], report["restored"])


if __name__ == "__main__":
    raise SystemExit(main())
