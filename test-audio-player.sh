#!/bin/sh
set -eu
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -f "$TEST_DIR"/*; rmdir "$TEST_DIR"' EXIT
# 主机没有 opus 头/静态库: 用 dlopen 路径测(板上才走静态链; XR_OPUS_INC/LIB 可覆盖)
"${HOST_CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -DXR_PLAYER_USE_DLOPEN_OPUS=1 -I"$DIR/board" \
    "$DIR/board/audio_player.c" "$DIR/board/xr_volume.c" "$DIR/board/audio_player_test.c" \
    -ldl -lpthread -o "$TEST_DIR/player"
"$TEST_DIR/player"
# 墙钟回拨回归(Codex review P2, 2026-11-20): REALTIME 读数前移 1h,
# 内核等待时钟不变 —— 全部测试在此注入下仍须通过(等待一律 MONOTONIC)
"${HOST_CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -DXR_PLAYER_USE_DLOPEN_OPUS=1 -I"$DIR/board" \
    "$DIR/board/audio_player.c" "$DIR/board/xr_volume.c" "$DIR/board/audio_player_test.c" \
    "$DIR/board/clockskew_wrap.c" -Wl,--wrap=clock_gettime \
    -ldl -lpthread -o "$TEST_DIR/player_clockskew"
"$TEST_DIR/player_clockskew"
# spk_open 失败路径(句柄泄漏 / 复用分支吞错误码): 成功路径测不到, 单独一个门禁
"${HOST_CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -DXR_PLAYER_USE_DLOPEN_OPUS=1 -I"$DIR/board" \
    "$DIR/board/audio_player.c" "$DIR/board/xr_volume.c" "$DIR/board/audio_player_open_test.c" \
    -ldl -lpthread -o "$TEST_DIR/player_open"
"$TEST_DIR/player_open"
# P3 FIFO 让位状态机(无硬件): 环与让位态转换
"${HOST_CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -DXR_PLAYER_USE_DLOPEN_OPUS=1 -I"$DIR/board" \
    "$DIR/board/audio_player.c" "$DIR/board/xr_volume.c" "$DIR/board/audio_player_yield_test.c" \
    -ldl -lpthread -o "$TEST_DIR/yield"
XR_TEST_REAL_RESUME_SCRIPT="$DIR/ports/rk3562/snapclient-alsa-resume.sh" "$TEST_DIR/yield"
"${HOST_CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/audio_capture.c" "$DIR/board/audio_capture_test.c" \
    -ldl -o "$TEST_DIR/capture"
"$TEST_DIR/capture"
