#!/bin/sh
# Host-side one-shot voice trial: board mic ASR -> board WebSocket/Opus playback.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
DURATION="${1:-30}"
SPEAKER="${XZ_BOARD_SPEAKER:-plughw:1,0}"
case "$DURATION" in
    ''|*[!0-9]*) echo "duration must be whole seconds" >&2; exit 2 ;;
esac
[ "$DURATION" -ge 3 ] && [ "$DURATION" -le 120 ] || { echo "duration must be 3..120 s" >&2; exit 2; }

"$DIR/build-board-smoke.sh"
adb ${SERIAL:+-s $SERIAL} reverse tcp:8000 tcp:8000
adb ${SERIAL:+-s $SERIAL} push "$DIR/board/xz_board_smoke" /tmp/xz_board_smoke
adb ${SERIAL:+-s $SERIAL} shell 'chmod 755 /tmp/xz_board_smoke'

echo "[test] 正在加载板端识别器；看到‘现在请说话’后开口，采集窗口 ${DURATION} s"
echo "[test] 出现 listening on asr_mic 后请说完整一句，停顿 1～2 秒"
adb ${SERIAL:+-s $SERIAL} shell "cd /data/rknn-test/sherpa && LD_LIBRARY_PATH=\$PWD/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib ./sherpa_live ./model ./silero_vad.onnx asr_mic $DURATION /tmp/xz_board_asr_capture.pcm 2>&1 | tee /tmp/xz_board_asr.log"
ASR_LOG="$(adb ${SERIAL:+-s $SERIAL} shell 'cat /tmp/xz_board_asr.log')"
TEXT="$(printf '%s\n' "$ASR_LOG" | sed -n 's/^\[[0-9][0-9]*\].* -> \(.*\)  (decode [0-9][0-9]* ms)$/\1/p' | head -1)"
if [ -z "$TEXT" ]; then
    echo "[test] 没有识别出完整语句；本次原始板麦采样保存在板上 /tmp/xz_board_asr_capture.pcm，日志在 /tmp/xz_board_asr.log" >&2
    exit 3
fi
echo "[test] 发送板端识别结果：$TEXT"
printf '%s\n' "$TEXT" | adb ${SERIAL:+-s $SERIAL} shell "/tmp/xz_board_smoke - $SPEAKER"
