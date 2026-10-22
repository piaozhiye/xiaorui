#!/bin/sh
# Build the vendored plugin only; never installs on the board or changes SDK sources.
set -eu
REPO="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SDK="${XR_VQE_SDK:-$HOME/rockaa/rockaa/rkaudio_vqe}"
TC="${XR_TOOLCHAIN_ROOT:-$HOME/projects/toolchains/gcc-arm-buildroot-aarch}"
OUT="${1:-$REPO/vendor/rkaudio-vqe/libasound_module_pcm_rkaudio_vqe.so}"
export LD_LIBRARY_PATH="$TC/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$TC/bin/aarch64-buildroot-linux-gnu-gcc" -std=gnu11 -D_GNU_SOURCE -DPIC \
    -march=armv8-a -O2 -g -fPIC -shared -I"$REPO/vendor/rkaudio-vqe" -I"$SDK/include" \
    -I"$SDK/prebuilt/linux-aarch64/include" \
    "$REPO/vendor/rkaudio-vqe/pcm_rkaudio_vqe.c" "$SDK/src/rkaudio_debug.c" \
    -L"$SDK/prebuilt/linux-aarch64/lib" -lasound -lrkaudio_vqe -lrkaudio_common \
    -lrkaudio_nn -lpthread -lrt -lm -o "$OUT"
sha256sum "$OUT"
