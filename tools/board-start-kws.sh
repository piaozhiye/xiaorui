#!/bin/sh
set -eu
# 板端: FIFO 麦克风 + 真实 KWS/ASR; 先起喂流进程(阻塞等读者), 再起前端
FEED=${1:-/tmp/kws_cycle.raw}
LOOPS=${2:-1}
STARTUP_SILENCE_MS=${3:-5000}
TAIL_SILENCE_MS=${4:-1000}
FIFO=/tmp/mic.fifo
case "$LOOPS" in ''|*[!0-9]*) echo "loops must be a positive integer" >&2; exit 2;; esac
[ "$LOOPS" -gt 0 ] || { echo "loops must be a positive integer" >&2; exit 2; }
case "$STARTUP_SILENCE_MS" in ''|*[!0-9]*) echo "startup silence must be a non-negative integer" >&2; exit 2;; esac
[ "$((STARTUP_SILENCE_MS % 100))" -eq 0 ] || { echo "startup silence must be a multiple of 100 ms" >&2; exit 2; }
case "$TAIL_SILENCE_MS" in ''|*[!0-9]*) echo "tail silence must be a non-negative integer" >&2; exit 2;; esac
[ "$((TAIL_SILENCE_MS % 100))" -eq 0 ] || { echo "tail silence must be a multiple of 100 ms" >&2; exit 2; }
[ -r "$FEED" ] || { echo "fixture is not readable: $FEED" >&2; exit 2; }
pkill -x xiaorui_live 2>/dev/null || true
pkill -f '[m]ic-feed.py' 2>/dev/null || true
sleep 2
rm -f "$FIFO" /tmp/mic-feed.log /tmp/kws-e2e.pids
mkfifo "$FIFO"
setsid /data/gwvenv/bin/python /data/xiaorui/mic-feed.py "$FEED" "$FIFO" "$LOOPS" "$STARTUP_SILENCE_MS" "$TAIL_SILENCE_MS" > /tmp/mic-feed.log 2>&1 &
FEED_PID=$!
sleep 0.5
: > /data/xiaorui/frontend.log
. /data/xiaorui/xiaorui.env
export XR_MIC="fifo:$FIFO"
export XR_VAD_TRIGGER_DB=6
export XR_TIMING=1
setsid /data/xiaorui/bin/xiaorui_live >>/data/xiaorui/frontend.log 2>&1 </dev/null &
FRONTEND_PID=$!
echo "$FEED_PID $FRONTEND_PID" > /tmp/kws-e2e.pids
echo "feed pid $FEED_PID; frontend pid $FRONTEND_PID"
