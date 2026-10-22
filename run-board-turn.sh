#!/bin/sh
# Host launcher for one RK3576 microphone -> Xiaozhi -> speaker turn.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
"$DIR/build-board-smoke.sh"
adb ${SERIAL:+-s $SERIAL} reverse tcp:8000 tcp:8000
adb ${SERIAL:+-s $SERIAL} push "$DIR/board/xz_board_smoke" /tmp/xz_board_smoke
adb ${SERIAL:+-s $SERIAL} push "$DIR/board/xr_board_turn.sh" /tmp/xr_board_turn.sh
adb ${SERIAL:+-s $SERIAL} shell 'chmod 755 /tmp/xz_board_smoke /tmp/xr_board_turn.sh'
echo "[host] 板端会倒数 3 秒后录音 8 秒；请靠近 USB 麦克风准备说话"
adb ${SERIAL:+-s $SERIAL} shell '/tmp/xr_board_turn.sh 8'
