#!/bin/sh
# Build the RK3576 live frontend.  Keep the complete source list here so a
# shared transport-layer change cannot accidentally be omitted from a build.
set -eu

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TOOLCHAIN_ROOT="${XR_TOOLCHAIN_ROOT:-$HOME/projects/toolchains/gcc-arm-buildroot-aarch}"
CC="${CC:-$TOOLCHAIN_ROOT/bin/aarch64-buildroot-linux-gnu-gcc}"
OPUS_ROOT="${XR_OPUS_ROOT:-/tmp/opus-aarch64}"
CC_ROOT="$(dirname "$(dirname "$(readlink -f "$CC")")")"

if [ -d "$CC_ROOT/lib" ]; then
    export LD_LIBRARY_PATH="$CC_ROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# -ffile-prefix-map: 把构建目录前缀钉成字面量, 使**同源码不同目录**构建出的
#   整文件 SHA 相同(-g 里原本嵌绝对路径)。这是验收脚本 require_candidate
#   三方 SHA 门禁成立的前提: 本机构建 / 候选 / 生产要逐位相等, 而门禁本地
#   构建目录一旦不是这个路径, 调试路径就会让 SHA 必然失配。GCC 8+。
# -march=armv8-a: 本工具链默认架构含 LSE, stdatomic 的 CAS 会编成 casal,
#   RK3576(A72/A53, ARMv8.0 无 LSE)上即 SIGILL -- 2026-10-16 语音重启失败根因
#   (xr_request_reboot 首条指令 0x404c74 casal, fork 都没到)。防回归另见
#   tools/check-no-lse.sh 门禁。
"$CC" -std=c11 -O2 -g -Wall -Wextra -march=armv8-a ${XR_CFLAGS_EXTRA:-} \
    -ffile-prefix-map="$DIR"=. -Werror \
    -I"$DIR/board" -I"$OPUS_ROOT/include/opus" \
    "$DIR/board/xiaorui_live.c" \
    "$DIR/board/audio_player.c" \
    "$DIR/board/audio_capture.c" \
    "$DIR/board/audio_cue.c" \
    "$DIR/board/music_controls.c" \
    "$DIR/board/playback_control.c" \
    "$DIR/board/speech_pipeline.c" \
    "$DIR/board/voice_session.c" \
    "$DIR/board/xr_vad.c" \
    "$DIR/board/xr_mic4.c" \
    "$DIR/board/xr_volume.c" \
    "$DIR/board/backend_local.c" \
    "$DIR/board/backend_cloud.c" \
    "$DIR/board/backend_hybrid.c" \
    "$DIR/board/ws_transport.c" \
    "$DIR/board/runtime_config.c" \
    "$OPUS_ROOT/lib/libopus.a" -lm -ldl -lpthread \
    -o "$DIR/board/xiaorui_live"

file "$DIR/board/xiaorui_live"
