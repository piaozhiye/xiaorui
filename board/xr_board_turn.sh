#!/bin/sh
# One board-side half-duplex turn: ALSA mic/WAV -> RKNN Paraformer -> Xiaozhi -> ALSA speaker.
set -eu
ROOT=/data/rknn-test/sherpa
RUNTIME="$ROOT/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared"
PLAYER=/data/xiaorui-server/board-scripts/xz_board_smoke
SPEAKER="${XZ_BOARD_SPEAKER:-plughw:1,0}"
MIC="${XZ_BOARD_MIC:-plughw:2,0}"
DELAY="${XZ_BOARD_DELAY:-3}"
CAPTURE=/tmp/xz_turn_capture.wav

if [ "${1:-}" = --wav ]; then
    [ "$#" -eq 2 ] || { echo "usage: $0 --wav /path/to/16k-mono.wav" >&2; exit 2; }
    WAV=$2
else
    DURATION="${1:-8}"
    case "$DURATION" in ''|*[!0-9]*) echo "duration must be 1..10 s" >&2; exit 2;; esac
    [ "$DURATION" -ge 1 ] && [ "$DURATION" -le 10 ] || { echo "duration must be 1..10 s" >&2; exit 2; }
    echo "[board-turn] 等待说话(最长 ${DURATION}s, 说完自动停)" >&2
    # 单进程直采: xz_capture 自己 readi + 帧级 VAD, 说完 0.4s 即停 (无 arecord 开销)
    if ! /data/xiaorui-server/board-scripts/xz_capture \
            --out "$CAPTURE" --wait "$DURATION" --max "${XZ_BOARD_RECORD_SECS:-12}" >&2; then
        rc=$?
        [ "$rc" = 4 ] && { echo "[board-turn] ${DURATION}s 内未检测到说话" >&2; exit 4; }
        echo "[board-turn] 采集失败 rc=$rc" >&2
        exit 2
    fi
    WAV=$CAPTURE
fi
[ -r "$WAV" ] || { echo "WAV missing: $WAV" >&2; exit 2; }
[ -x "$PLAYER" ] || { echo "player missing: $PLAYER" >&2; exit 2; }
if ! "$PLAYER" --check-level "$WAV"; then
    echo "[board-turn] 音频电平过低；未调用 ASR，避免把静音误听成指令" >&2
    exit 4
fi

echo "[board-turn] recognizing $WAV" >&2
ASR_OUTPUT="$(cd "$ROOT" && LD_LIBRARY_PATH="$RUNTIME/lib:/usr/lib" \
    "$RUNTIME/bin/sherpa-onnx-offline" \
    --tokens=./model/tokens.txt \
    --paraformer=./model/encoder.rknn,./model/predictor.rknn,./model/decoder.rknn \
    --provider=rknn --model-type=paraformer "$WAV" 2>&1)"
TEXT="$(printf '%s\n' "$ASR_OUTPUT" | sed -n 's/.*"text": "\([^"]*\)".*/\1/p' | tail -1)"
if [ -z "$TEXT" ]; then
    echo "[board-turn] no ASR text" >&2
    printf '%s\n' "$ASR_OUTPUT" | tail -15 >&2
    exit 3
fi
printf '[board-turn] ASR: %s\n' "$TEXT"
if [ "${XZ_BOARD_GATE:-0}" = "1" ] && ! printf '%s' "$TEXT" | grep -qE "小."; then
    echo "[board-turn] 未含唤醒词, 忽略(关掉 XZ_BOARD_GATE=0 可全响应)"
    exit 42
fi
printf '%s\n' "$TEXT" | "$PLAYER" - "$SPEAKER"
