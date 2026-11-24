#!/bin/sh
# 板端: 起"正常前端 + 音乐 FIFO + 文本循环注入"用于真实后端 E2E
# 用法: start-e2e.sh <文本> <轮数> <间隔ms>
TEXT=${1:-你好}
N=${2:-3}
GAP=${3:-1500}
stop_bridge() {
    /data/gwvenv/bin/python - /data/xiaorui/snapcast/snapcast-fifo-channel.py <<'PY'
import os, pathlib, signal, sys, time
target = sys.argv[1]
pids = []
for entry in pathlib.Path('/proc').iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        argv = (entry / 'cmdline').read_bytes().split(b'\0')
    except OSError:
        continue
    if len(argv) > 1 and os.fsdecode(argv[1]) == target:
        pids.append(int(entry.name))
for pid in pids:
    try: os.kill(pid, signal.SIGTERM)
    except ProcessLookupError: pass
deadline = time.monotonic() + 2
alive = set(pids)
while alive and time.monotonic() < deadline:
    alive = {pid for pid in alive if pathlib.Path('/proc/%d' % pid).exists()}
    if alive: time.sleep(0.05)
for pid in alive:
    try: os.kill(pid, signal.SIGKILL)
    except ProcessLookupError: pass
PY
}
pkill -x xiaorui_live 2>/dev/null
pkill -x snapclient 2>/dev/null
stop_bridge
sleep 2
: > /data/xiaorui/frontend.log
. /data/xiaorui/xiaorui.env
export XR_MUSIC_FIFO=/data/xiaorui/snapcast/music.fifo
export XR_TEXT_INJECT="$TEXT"
export XR_TEXT_INJECT_REPEAT="$N"
export XR_TEXT_INJECT_GAP_MS="$GAP"
export XR_VAD_TRIGGER_DB=20
export XR_TIMING=1
setsid /data/xiaorui/bin/xiaorui_live >>/data/xiaorui/frontend.log 2>&1 </dev/null &
echo "frontend started pid $!"
