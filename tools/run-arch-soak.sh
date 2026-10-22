#!/bin/bash
# 候选网关 30 分钟静音浸泡: 探针常驻(announce_ack), 周期活轮+受理投递, fd/RSS 采样.
set -u
cd "$(dirname "$0")/.."
V=$HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python

SOAK_URL="${SOAK_URL:-ws://127.0.0.1:28001/xiaorui/v1/}"
SOAK_INST="${SOAK_INST:-cand-18001}"
$V - "$SOAK_URL" "$SOAK_INST" <<'PYEOF'
import json, os, subprocess, sys, time
from websockets.sync.client import connect
sys.path.insert(0, "tools")
from soak_health import HealthGuard, cleanup_confirmed
SOAK_URL, SOAK_INST = sys.argv[1], sys.argv[2]

LOG = open("/tmp/arch-soak.log", "w", buffering=1)
_SER = os.environ.get("XZ_BOARD_SERIAL", "")
ADB = ["adb", "-s", _SER, "shell"] if _SER else ["adb", "shell"]
health = HealthGuard()

def sh(cmd):
    return subprocess.run(ADB + [cmd], capture_output=True, text=True,
                          timeout=10, check=True).stdout.strip()

def log(msg):
    LOG.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

def sample(tag):
    p1 = sh("cat /data/xiaorui-gateway/instances/%s/run/server.pid" % SOAK_INST)
    fp = sh("pgrep -x xiaorui_live")
    if not p1.isdigit() or not fp.isdigit():
        raise RuntimeError("expected exactly one gateway PID and one production frontend PID")
    boot = sh("cat /proc/sys/kernel/random/boot_id")
    for name, pid in ((SOAK_INST, p1), ("frontend", fp)):
        # starttime guards PID reuse; comm may contain spaces, so split after ')'.
        stat = sh(f"cat /proc/{pid}/stat")
        start = stat.rsplit(")", 1)[1].split()[19]
        rss = sh(f"awk '/VmRSS/ {{print $2}}' /proc/{pid}/status")
        links = sh(f"ls -l /proc/{pid}/fd")
        targets = [line.split(" -> ", 1)[1] for line in links.splitlines() if " -> " in line]
        fd = len(targets)
        dma = sum(target.startswith("/dmabuf:") or target.startswith("anon_inode:dmabuf")
                  for target in targets)
        identity = f"{boot}:{pid}:{start}"
        if name == "frontend":
            tick, generation = map(int, sh(f"cat /tmp/xr-live-health-{pid}").split())
            identity += f":{generation}"  # exec keeps PID/starttime, but resets generation
            # /proc/uptime is rounded down to centiseconds, unlike the C tick.
            now_ms = int(float(sh("cut -d' ' -f1 /proc/uptime")) * 1000) + 20
            health.check_progress(tick, now_ms)
        health.check(name, identity, int(rss), fd, dma=dma)
        log(f"sample[{tag}] {name} pid={pid} rss_kb={rss} fd={fd} dma={dma} ordinary={fd-dma}")

def turn(ws, cmd):
    ws.send(json.dumps({"type": "listen", "state": "detect", "text": cmd}))
    accepted, texts, packets = False, [], 0
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            d = ws.recv(timeout=max(.1, deadline - time.monotonic()))
        except TimeoutError:
            continue
        if isinstance(d, bytes):
            if accepted: packets += 1
            continue
        e = json.loads(d)
        if e.get("type") == "stt" and e.get("text") == cmd:
            accepted = True
        if not accepted: continue
        if e.get("type") == "tts" and e.get("state") == "sentence_start":
            texts.append(e.get("text", ""))
        if e.get("type") == "tts" and e.get("state") == "stop" and packets:
            return "".join(texts)
    return None

try:
    log("limits ordinary_fd_growth=8 dma_warmup_growth=96 warmup_s=120 steady_dma_growth=8 rss_growth_kb=131072; protocol soak, NOT acoustic validation")
    if not SOAK_INST or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in SOAK_INST):
        raise ValueError("invalid SOAK_INST")
    sample("baseline")
    ws = connect(SOAK_URL, additional_headers={"device-id": "arch-soak-probe"},
                 open_timeout=10, close_timeout=3)
    ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
        "features": {"mcp": False, "aec": False, "announce_ack": True}, "audio_params": {
        "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
    while True:
        d = ws.recv(timeout=15)
        if isinstance(d, str) and json.loads(d).get("type") == "hello":
            break
    log("connected")
    # 清理本探针设备的历史残留(被杀的旧 soak 会留下过期定时器, 一连上就被
    # 调度器当作积压投递, 污染本轮判定); 探针专用设备, 不碰生产设备条目
    r = turn(ws, "取消提醒")
    log(f"stale-timer cleanup reply: {r}")
    if not cleanup_confirmed(r):
        raise RuntimeError("probe-device timer cleanup was not confirmed")
    # t=0: 设一个 240s 提醒(浸泡中期由本探针受理投递), 再周期活轮
    r = turn(ws, "四分钟后提醒我")
    log(f"timer-set reply: {r}")
    if not r or "提醒你" not in r:
        log("SOAK-ERROR timer set failed")
        sys.exit(2)
    deliveries = 0
    grant = False
    delivery_text = False
    delivery_packets = 0
    offered_id = None
    t0 = time.monotonic()
    for i in range(7):   # 7 轮 x ~5 min = 30 min
        sample(i)
        r = turn(ws, "有什么提醒")
        log(f"round{i} list: {r}")
        if r is None:
            log("SOAK-ERROR liveness turn timed out")
            sys.exit(2)
        # 受理窗口: 收/答任何 pending offer(240s 提醒到点后在此被受理)
        # 周期 ping 保活: 服务端 ~180s 无活动会以 1000 关闭连接(实测 187s)
        deadline = time.monotonic() + 260  # 7*260 >= 30 minutes, excluding active turns
        last_ping = time.monotonic()
        last_sample = time.monotonic()
        while time.monotonic() < deadline:
            try:
                d = ws.recv(timeout=2)
            except TimeoutError:
                d = None
            if time.monotonic() - last_ping > 30:
                ws.send(json.dumps({"type": "ping"}))
                last_ping = time.monotonic()
            if time.monotonic() - last_sample > 30:
                sample(f"{i}-periodic")
                last_sample = time.monotonic()
            if d is None:
                continue
            if isinstance(d, bytes):
                if grant: delivery_packets += 1
                continue
            if isinstance(d, str):
                e = json.loads(d)
                if e.get("type") == "announce" and e.get("state") == "offer":
                    offered_id = e["request_id"]
                    ws.send(json.dumps({"type": "listen", "state": "announce_ack",
                                        "request_id": e["request_id"], "accepted": True}))
                if e.get("type") == "announce" and e.get("state") == "grant":
                    if grant or not offered_id or e.get("request_id") != offered_id:
                        raise RuntimeError("unmatched/overlapping announcement grant")
                    grant = True
                    delivery_text = False
                    delivery_packets = 0
                if e.get("type") == "tts" and e.get("state") == "sentence_start" and "时间到了" in e.get("text", ""):
                    if not grant:
                        raise RuntimeError("reminder text without a granted announcement")
                    if not delivery_text:
                        delivery_packets = 0  # don't credit preceding user-turn audio
                    delivery_text = True
                if e.get("type") == "tts" and e.get("state") == "stop" and grant:
                    if not delivery_text:
                        # 调度器可能在用户轮(list 查询)流式中投递 offer: 该轮自己的
                        # stop 先到, 不代表投递失败; 真失败的投递由收尾 not grant 兜底
                        log(f"concurrent-turn stop ignored (round{i})")
                        continue
                    if not delivery_packets:
                        raise RuntimeError("announcement had reminder text but no audio")
                    deliveries += 1
                    grant = False
                    log(f"delivery completed (round{i}, packets={delivery_packets})")
    sample("final")
    log(f"SOAK-DONE deliveries={deliveries} elapsed={int(time.monotonic()-t0)}s")
    ws.close()
    sys.exit(0 if deliveries == 1 and not grant and time.monotonic()-t0 >= 1800 else 3)
except Exception as e:
    log(f"SOAK-ERROR {type(e).__name__}: {e}")
    sys.exit(1)
PYEOF
rc=$?
tail -30 /tmp/arch-soak.log
echo "SOAK rc=$rc"
exit $rc
