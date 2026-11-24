#!/bin/bash
# 交叉编译 libopus(aarch64, 静态库), 供 build-board-live.sh 链接。
#
# 用法:
#   XR_TOOLCHAIN_ROOT=<aarch64 Buildroot 工具链> tools/build-opus-aarch64.sh [输出目录]
#   # 默认输出 /tmp/opus-aarch64 (build-board-live.sh 的 XR_OPUS_ROOT 默认值)
#
# 产物: <out>/lib/libopus.a + <out>/include/opus/*.h
set -euo pipefail

OUT="${1:-${XR_OPUS_ROOT:-/tmp/opus-aarch64}}"
VER="${OPUS_VERSION:-1.4}"
WORK="${OPUS_WORK:-/tmp/opencode}"
: "${XR_TOOLCHAIN_ROOT:?设置 XR_TOOLCHAIN_ROOT=aarch64 Buildroot 工具链根目录}"

export LD_LIBRARY_PATH="$XR_TOOLCHAIN_ROOT/lib:$XR_TOOLCHAIN_ROOT/lib64:${LD_LIBRARY_PATH:-}"
mkdir -p "$WORK" "$OUT/lib" "$OUT/include/opus"

if [ ! -d "$WORK/opus-$VER" ]; then
    echo "== 下载 opus $VER =="
    curl -sSL --fail -o "$WORK/opus.tar.gz" \
        "https://downloads.xiph.org/releases/opus/opus-$VER.tar.gz"
    tar xzf "$WORK/opus.tar.gz" -C "$WORK"
fi

# 自带工具链文件(不依赖 /tmp 里别的脚本产物)
TC_FILE="$WORK/toolchain-aarch64.cmake"
cat > "$TC_FILE" <<EOF
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(TOOLCHAIN_ROOT "$XR_TOOLCHAIN_ROOT")
set(CMAKE_C_COMPILER   "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-gcc")
set(CMAKE_CXX_COMPILER "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-g++")
set(CMAKE_AR     "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-ar" CACHE FILEPATH "")
set(CMAKE_RANLIB "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-ranlib" CACHE FILEPATH "")
set(CMAKE_FIND_ROOT_PATH "\${TOOLCHAIN_ROOT}/aarch64-buildroot-linux-gnu/sysroot")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_C_FLAGS_INIT "-march=armv8-a")
EOF

cmake -S "$WORK/opus-$VER" -B "$WORK/opus-build" \
    -DCMAKE_TOOLCHAIN_FILE="$TC_FILE" -DCMAKE_BUILD_TYPE=Release \
    -DOPUS_BUILD_TESTING=OFF -DOPUS_BUILD_PROGRAMS=OFF -DBUILD_SHARED_LIBS=OFF >/dev/null
cmake --build "$WORK/opus-build" -j "$(nproc)" >/dev/null

cp "$WORK/opus-build/libopus.a" "$OUT/lib/"
cp "$WORK/opus-$VER/include/"*.h "$OUT/include/opus/"
echo "ok: $OUT/lib/libopus.a"
echo "    $OUT/include/opus/ ($(ls "$OUT/include/opus" | wc -l) 个头文件)"
