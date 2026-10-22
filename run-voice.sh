#!/bin/sh
# 麦克风语音客户端: 麦克风 -> 端侧 KWS/ASR -> 服务端 -> TTS 回程播放
# 采集默认内置数字麦阵列 plughw:2,0(XR_MIC 换); 播放默认模拟口 plughw:1,0
# (ALSA default 走 HDMI, 没接显示器喇叭时无声; XZ_SPEAKER 可换)
DIR="$(cd "$(dirname "$0")" && pwd)"
SRV="${XZ_SERVER_DIR:-$HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server}"
if [ ! -x "$SRV/.venv/bin/python" ]; then
    echo "服务端不在 $SRV —— 先按 $DIR/README.md 部署" >&2
    exit 1
fi
LD_LIBRARY_PATH="$SRV/.nativelib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    exec "$SRV/.venv/bin/python" "$DIR/xz_voice.py" \
    --device "${XR_MIC:-plughw:2,0}" --speaker "${XZ_SPEAKER:-plughw:1,0}" "$@"
