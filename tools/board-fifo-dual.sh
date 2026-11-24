#!/bin/sh
# 板端: Snapclient(48k stereo) -> music.raw -> bridge -> music.fifo(24k mono) -> 前端
# channel=0/1 用于双频同步探针；channel=mix 用于音乐，保留完整 L/R 内容。
# 用法: sh /data/xiaorui/snapcast/start-fifo-dual.sh <0|1|mix> [server]
CH=${1:?用法: start-fifo-dual.sh <0|1|mix> [server]}
SRV=${2:-127.0.0.1:17104}
cd /data/xiaorui/snapcast || exit 1

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

pkill -x snapclient 2>/dev/null
stop_bridge
sleep 1

rm -f music.raw
mkfifo music.raw || { echo "mkfifo 失败"; exit 1; }
ls -l music.raw

# bridge 先起(它是 music.raw 的读者, snapclient 才能 open)
setsid /data/gwvenv/bin/python /data/xiaorui/snapcast/snapcast-fifo-channel.py \
    /data/xiaorui/snapcast/music.raw /data/xiaorui/snapcast/music.fifo "$CH" \
    > /tmp/bridge.log 2>&1 &
BRIDGE_PID=$!
sleep 1
# snapclient 后起(写 music.raw)
setsid /data/xiaorui/snapcast/snapclient "tcp://$SRV" \
    --player="file:filename=/data/xiaorui/snapcast/music.raw" \
    --mixer software --logsink stdout --logfilter '*:warn' > /tmp/sc-dual.log 2>&1 &
sleep 3
echo "bridge: $BRIDGE_PID  client: $(pgrep -x snapclient | head -1)"
