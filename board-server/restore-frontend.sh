#!/bin/sh
# Restore only the production frontend; do not restart gateway/LLM services.
set -eu
PROD=/data/xiaorui-server/board-scripts/xiaorui_live
CAND=/data/xiaorui-gateway/xiaorui_live-cand
LOG=/tmp/xr_live.log

for proc in /proc/[0-9]*; do
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    if [ "$exe" = "$CAND" ]; then
        pid=${proc#/proc/}
        kill -TERM "$pid" 2>/dev/null || true
        i=0
        while [ "$(readlink "$proc/exe" 2>/dev/null || true)" = "$CAND" ]; do
            i=$((i+1)); [ "$i" -lt 15 ] || { echo 'FAIL candidate did not exit'; exit 1; }
            sleep 1
        done
    fi
done

PID=
for proc in /proc/[0-9]*; do
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    if [ "$exe" = "$PROD" ]; then
        [ -z "$PID" ] || { echo 'FAIL multiple production frontends'; exit 1; }
        PID=${proc#/proc/}
    fi
done
if [ -z "$PID" ]; then
    cd /data/xiaorui-server
    . ./xiaorui.env
    setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib \
        "$PROD" > "$LOG" 2>&1 < /dev/null &
    PID=$!
fi
i=0
while [ "$i" -lt 45 ]; do
    # setsid/env may not have exec'd the binary in the first scheduling slice.
    if [ "$(readlink /proc/$PID/exe 2>/dev/null || true)" = "$PROD" ] &&
        grep -q '\[xr\] ambient=' "$LOG"; then
        echo "production ready pid=$PID"; exit 0
    fi
    sleep 1; i=$((i+1))
done
echo 'FAIL production calibration timed out'
exit 1
