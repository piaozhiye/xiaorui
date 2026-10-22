#!/bin/sh
# 语音服务端启动器(qwen 大脑+人设+EdgeTTS), 前台运行, Ctrl-C 停
DIR="$(cd "$(dirname "$0")" && pwd)"
SRV="${XZ_SERVER_DIR:-$HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server}"
if [ ! -x "$SRV/.venv/bin/python" ]; then
    echo "服务端不在 $SRV —— 先按 $DIR/README.md 部署" >&2
    exit 1
fi
# 上游 bug: LLM 插件用相对路径定位(core/providers/...), 必须从服务端目录启动
cd "$SRV" || exit 1
exec "$SRV/.venv/bin/python" "$SRV/app.py"
