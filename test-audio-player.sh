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
# spk_open 失败路径(句柄泄漏 / 复用分支吞错误码): 成功路径测不到, 单独一个门禁
"${HOST_CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -DXR_PLAYER_USE_DLOPEN_OPUS=1 -I"$DIR/board" \
    "$DIR/board/audio_player.c" "$DIR/board/xr_volume.c" "$DIR/board/audio_player_open_test.c" \
    -ldl -lpthread -o "$TEST_DIR/player_open"
"$TEST_DIR/player_open"
"${HOST_CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/audio_capture.c" "$DIR/board/audio_capture_test.c" \
    -ldl -o "$TEST_DIR/capture"
"$TEST_DIR/capture"
