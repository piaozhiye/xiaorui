#!/bin/sh
# 语音客户端启动器: 借服务端 venv(websockets/opuslib_next) + 免 root libopus
# 用法: run-client.sh [--url ws://...:8000/xiaorui/v1/] [--device-id AA:BB:...]
DIR="$(cd "$(dirname "$0")" && pwd)"
SRV="${XZ_SERVER_DIR:-$HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server}"
if [ ! -x "$SRV/.venv/bin/python" ]; then
    echo "服务端不在 $SRV —— 先按 $DIR/README.md 部署" >&2
    exit 1
fi
LD_LIBRARY_PATH="$SRV/.nativelib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    exec "$SRV/.venv/bin/python" "$DIR/xz_client.py" "$@"
