#!/bin/sh
# 双麦阵列逐路诊断: 录 8s 原生双通道 -> 拆 ch0/ch1/下混 -> 各自过端侧 ASR+KWS
# 用途: 验证 plughw 下混是否在抵消语音(症状: 唤醒词时好时坏、识别碎片化)
DIR="$(cd "$(dirname "$0")" && pwd)"
SRV="${XZ_SERVER_DIR:-$HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server}"
PY="$SRV/.venv/bin/python"
echo ">>> 3 秒后开始录 8 秒, 请对着笔记本连续喊「小瑞小瑞」..."
sleep 3
arecord -D hw:2,0 -f S32_LE -r 48000 -c 2 -d 8 -t raw -q > /tmp/mic2.raw
echo ">>> 录音完成, 拆通道分析:"
ffmpeg -v error -y -f s32le -ar 48000 -ac 2 -i /tmp/mic2.raw -af "pan=mono|c0=c0" -ar 16000 -c:a pcm_s16le /tmp/mic2_ch0.wav
ffmpeg -v error -y -f s32le -ar 48000 -ac 2 -i /tmp/mic2.raw -af "pan=mono|c0=c1" -ar 16000 -c:a pcm_s16le /tmp/mic2_ch1.wav
ffmpeg -v error -y -f s32le -ar 48000 -ac 2 -i /tmp/mic2.raw -af "pan=mono|c0=0.5*c0+0.5*c1" -ar 16000 -c:a pcm_s16le /tmp/mic2_mix.wav
LD_LIBRARY_PATH="$SRV/.nativelib" "$PY" "$DIR/xz_voice.py" \
    --wav /tmp/mic2_ch0.wav /tmp/mic2_ch1.wav /tmp/mic2_mix.wav 2>&1 |
    grep -vE "handshake|done"
echo ">>> 判读: 哪一路 ASR/KWS 出了「小瑞(小子)小瑞(小子)」, 哪一路就是该用的采集方式"
