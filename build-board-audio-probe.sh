#!/bin/sh
# Reproducible board-side protocol/ASR probe build; does not deploy or open audio devices.
set -eu
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TOOLCHAIN_ROOT="${XR_TOOLCHAIN_ROOT:-$HOME/projects/toolchains/gcc-arm-buildroot-aarch}"
CC="${CC:-$TOOLCHAIN_ROOT/bin/aarch64-buildroot-linux-gnu-gcc}"
OPUS_ROOT="${XR_OPUS_ROOT:-/tmp/opus-aarch64}"
CC_ROOT="$(dirname "$(dirname "$(readlink -f "$CC")")")"
if [ -d "$CC_ROOT/lib" ]; then
    export LD_LIBRARY_PATH="$CC_ROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
# -march=armv8-a: 同 build-board-live.sh -- LSE 原子在 RK3576(ARMv8.0)上即 SIGILL
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -march=armv8-a \
    -I"$DIR/board" -I"$OPUS_ROOT/include/opus" \
    "$DIR/board/backend_audio_probe.c" "$DIR/board/backend_local.c" \
    "$DIR/board/backend_cloud.c" "$DIR/board/ws_transport.c" \
    "$DIR/board/runtime_config.c" "$OPUS_ROOT/lib/libopus.a" \
    -lm -ldl -o "$DIR/board/backend_audio_probe"
file "$DIR/board/backend_audio_probe"
