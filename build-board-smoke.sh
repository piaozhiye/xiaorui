#!/bin/sh
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
CC="${CC:-$HOME/projects/toolchains/gcc-arm-buildroot-aarch/bin/aarch64-buildroot-linux-gnu-gcc}"
CC_ROOT="$(dirname "$(dirname "$(readlink -f "$CC")")")"
if [ -d "$CC_ROOT/lib" ]; then
    export LD_LIBRARY_PATH="$CC_ROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
"$CC" -O2 -Wall -Wextra -march=armv8-a -o "$DIR/board/xr_board_smoke" "$DIR/board/xr_board_smoke.c" -ldl -lm
file "$DIR/board/xr_board_smoke"
