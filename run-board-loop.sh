#!/bin/sh
# Host launcher: 常驻板端循环(录音->NPU识别->网关->播放), 默认 10 轮.
# 用法: run-board-loop.sh [轮数]   (环境变量同 run-board-turn.sh: XZ_BOARD_MIC/SPEAKER/DELAY)
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
ROUNDS="${1:-10}"
adb ${SERIAL:+-s $SERIAL} push "$DIR/board/xz_board_smoke" /tmp/xz_board_smoke > /dev/null
adb ${SERIAL:+-s $SERIAL} push "$DIR/board/xr_board_turn.sh" /tmp/xr_board_turn.sh > /dev/null
adb ${SERIAL:+-s $SERIAL} push "$DIR/board/xr_board_loop.sh" /tmp/xr_board_loop.sh > /dev/null
adb ${SERIAL:+-s $SERIAL} shell 'chmod 755 /tmp/xz_board_smoke /tmp/xr_board_turn.sh /tmp/xr_board_loop.sh'
echo "[host] 板端开始 $ROUNDS 轮循环, 每轮录音 8 秒, 请在提示后靠近 USB 麦克风说话"
adb ${SERIAL:+-s $SERIAL} shell "/tmp/xr_board_loop.sh $ROUNDS"
