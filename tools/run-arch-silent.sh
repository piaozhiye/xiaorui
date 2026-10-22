#!/bin/bash
# 静音验收(无声学): 路由切换 + 故障注入 + 孤儿检查. 全部走 ws 协议探针.
set -u
cd "$(dirname "$0")/.."
SER=${XZ_BOARD_SERIAL:-}
V=$HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python
PASS=0; FAIL=0
board() { adb ${SER:+-s $SER} shell "$1"; }
ok()  { PASS=$((PASS+1)); echo "PASS $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL $1"; }

# turn <url> <command>: 提交一条文本指令, 返回拼接回复(非零退出=失败)
turn() {
    $V - "$1" "$2" <<'PYEOF'
import json, sys, time
from websockets.sync.client import connect
url, cmd = sys.argv[1], sys.argv[2]
with connect(url, additional_headers={"device-id": "arch-fault-probe"},
             open_timeout=10, close_timeout=3) as ws:
    ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
        "features": {"mcp": False, "aec": False, "announce_ack": True}, "audio_params": {
        "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
    # 重启后首连会有欢迎语 offer: 先正常受理完, 避开 legacy 欢迎语竞态(已知边界)
    while True:
        data = ws.recv(timeout=10)
        if isinstance(data, str):
            ev = json.loads(data)
            if ev.get("type") == "hello":
                break
    import time as _t
    _deadline = _t.monotonic() + 8
    welcomed = False
    while _t.monotonic() < _deadline and not welcomed:
        try:
            data = ws.recv(timeout=1)
        except TimeoutError:
            break
        if isinstance(data, str):
            ev = json.loads(data)
            if ev.get("type") == "announce" and ev.get("state") == "offer":
                ws.send(json.dumps({"type": "listen", "state": "announce_ack",
                                    "request_id": ev["request_id"], "accepted": True}))
                welcomed = True
    if welcomed:
        _stop = _t.monotonic() + 45
        while _t.monotonic() < _stop:
            try:
                data = ws.recv(timeout=1)
            except TimeoutError:
                continue
            if isinstance(data, str):
                ev = json.loads(data)
                if ev.get("type") == "tts" and ev.get("state") == "stop":
                    break
    ws.send(json.dumps({"type": "listen", "state": "detect", "text": cmd}))
    accepted, texts, packets = False, [], 0
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            data = ws.recv(timeout=max(.1, deadline - time.monotonic()))
        except TimeoutError:
            continue
        if isinstance(data, bytes):
            if accepted:
                packets += 1
            continue
        event = json.loads(data)
        if event.get("type") == "stt" and event.get("text") == cmd:
            accepted = True
        if not accepted:
            continue
        if event.get("type") == "tts" and event.get("state") == "sentence_start":
            texts.append(event.get("text", ""))
        if event.get("type") == "tts" and event.get("state") == "stop":
            if not packets:
                raise RuntimeError("stop without audio")
            print("".join(texts))
            sys.exit(0)
    raise TimeoutError(cmd)
PYEOF
}

echo "== S1 断云: 18001 停, 18002 继续服务 =="
board 'kill $(cat /data/xiaorui-gateway/instances/cand-18001/run/server.pid) 2>/dev/null'
sleep 2
if reply=$(turn ws://127.0.0.1:28002/xiaorui/v1/ "有什么提醒") && echo "$reply" | grep -q "提醒"; then
    ok "S1-local-leg-serving ($reply)"
else
    bad "S1-local-leg-serving"
fi
if turn ws://127.0.0.1:28001/xiaorui/v1/ "有什么提醒" >/dev/null 2>&1; then
    bad "S1-dead-cloud-should-refuse"
else
    ok "S1-dead-cloud-refused"
fi

echo "== S2 轮次中杀网关: 连接异常关闭可观察, 不挂死 =="
PID2=$(board 'cat /data/xiaorui-gateway/instances/cand-18002/run/server.pid' | tr -d '\r\n ')
board "setsid sh -c 'sleep 3; kill $PID2' >/dev/null 2>&1 &"
if timeout 40 $V - <<'PYEOF'
import json, time
from websockets.sync.client import connect
try:
    with connect("ws://127.0.0.1:28002/xiaorui/v1/", additional_headers={"device-id": "arch-fault-probe"},
                 open_timeout=10, close_timeout=3) as ws:
        ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
            "features": {"mcp": False, "aec": False}, "audio_params": {
            "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
        while True:
            data = ws.recv(timeout=10)
            if isinstance(data, str) and json.loads(data).get("type") == "hello":
                break
        ws.send(json.dumps({"type": "listen", "state": "detect", "text": "用两百字讲讲长城"}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ws.recv(timeout=2)
        raise RuntimeError("kill not observable in 30s")
except RuntimeError:
    raise
except Exception:
    print("FAULT-OBSERVED")  # 连接异常关闭 = 可观察失败
PYEOF
then ok "S2-midturn-kill-observable"; else bad "S2-midturn-kill-observable"; fi

echo "== S3 孤儿/资源检查 + 双腿恢复 =="
board 'sh /data/xiaorui-gateway/start-gateway-instance.sh cand-18002' >/dev/null || bad "S3-restart-cand8002"
board 'sh /data/xiaorui-gateway/start-gateway-instance.sh cand-18001' >/dev/null || bad "S3-restart-cand18001"
sleep 10   # 稳态: 垂死网关的暂态子进程(ffmpeg/music_mcp)需数秒自行退出
orphans=$(board 'ps -eo pid,ppid,args | grep -E "ffmpeg|music_mcp" | grep -v grep | awk "\$2==1" | wc -l' | tr -d '\r\n ')
mcp=$(board 'ps -eo args | grep -c "python -m music_mcp"' | tr -d '\r\n ')
[ "${orphans:-0}" -eq 0 ] && ok "S3-no-orphan-children" || bad "S3-orphan-children=$orphans"
echo "info: music_mcp instances=$mcp (1 per live gateway conn expected)"
if reply=$(turn ws://127.0.0.1:28001/xiaorui/v1/ "有什么提醒") && echo "$reply" | grep -q "提醒"; then
    ok "S3-cloud-back"
else
    bad "S3-cloud-back"
fi

echo "================"
echo "SILENT pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
