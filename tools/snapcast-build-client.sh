#!/bin/bash
# 交叉编译 Snapcast snapclient (aarch64 / Buildroot), 供 RK3562 板端使用。
#
# 用法:
#   XR_TOOLCHAIN_ROOT=<aarch64 Buildroot 工具链根目录> \
#   XR_BOOST_ROOT=<boost 头文件前缀, 其下有 include/boost> \
#     tools/snapcast-build-client.sh [输出目录]
#
# 例:
#   XR_TOOLCHAIN_ROOT=/opt/gcc-arm-buildroot-aarch \
#   XR_BOOST_ROOT=/opt/boost-1.83 \
#     tools/snapcast-build-client.sh /tmp/snapcast-out
#
# 依赖:
#   - 工具链: aarch64-buildroot-linux-gnu-gcc/g++ (gcc 9.x), sysroot 里要有 alsa
#   - boost >= 1.74 的**头文件**即可: snapclient 只用 header-only 组件
#     (asio/beast/process/uuid), 不需要 program_options/system/filesystem 编译库
#   - cmake >= 3.16, make
#
# 开关说明(为什么这么选):
#   BUILD_WITH_VORBIS=ON   sysroot 有 libvorbis/libogg, 打开后能解 ogg/vorbis 流
#   BUILD_WITH_FLAC/OPUS=OFF  sysroot 没有 libFLAC/opus 的开发文件, 打开会直接 configure 失败
#   BUILD_WITH_AVAHI/EXPAT/SSL=OFF  减少依赖(snapclient 用不到)
#   BUILD_WITH_PULSE=OFF    板端走 ALSA / FIFO, 不引 libpulse
#   BUILD_SERVER=OFF        只需要客户端
#
# 产物: <输出目录>/snapclient (已 strip)
set -euo pipefail

OUT="${1:-/tmp/snapcast-client-out}"
SRC_VERSION="${SNAPCAST_VERSION:-v0.35.0}"
SRC_DIR="${SNAPCAST_SRC:-$OUT/src}"

: "${XR_TOOLCHAIN_ROOT:?设置 XR_TOOLCHAIN_ROOT=aarch64 Buildroot 工具链根目录}"
: "${XR_BOOST_ROOT:?设置 XR_BOOST_ROOT=boost 头文件前缀}"

TC="$XR_TOOLCHAIN_ROOT"
BOOST_INC="$XR_BOOST_ROOT/include"
[ -d "$BOOST_INC/boost" ] || { echo "boost 头文件不在 $BOOST_INC/boost" >&2; exit 2; }

# buildroot 版 gcc 自带 libmpfr, 运行时要从工具链目录里找
export LD_LIBRARY_PATH="$TC/lib:$TC/lib64:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT"

if [ ! -d "$SRC_DIR" ]; then
    echo "== 下载 snapcast $SRC_VERSION =="
    tarball="$OUT/snapcast-${SRC_VERSION}.tar.gz"
    # --fail: 404/重定向页面不能当 tarball 存下来(否则报错会很莫名其妙)
    curl -sSL --fail -o "$tarball" \
        "https://github.com/snapcast/snapcast/archive/refs/tags/${SRC_VERSION}.tar.gz"
    tar xzf "$tarball" -C "$OUT"
    mv "$OUT/snapcast-${SRC_VERSION#v}" "$SRC_DIR"
fi

cat > "$OUT/toolchain-aarch64.cmake" <<EOF
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(TOOLCHAIN_ROOT "$TC")
set(CMAKE_C_COMPILER   "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-gcc")
set(CMAKE_CXX_COMPILER "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-g++")
set(CMAKE_AR     "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-ar" CACHE FILEPATH "")
set(CMAKE_RANLIB "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-ranlib" CACHE FILEPATH "")
set(CMAKE_STRIP  "\${TOOLCHAIN_ROOT}/bin/aarch64-buildroot-linux-gnu-strip" CACHE FILEPATH "")
set(CMAKE_FIND_ROOT_PATH "\${TOOLCHAIN_ROOT}/aarch64-buildroot-linux-gnu/sysroot")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
set(CMAKE_C_FLAGS_INIT   "-march=armv8-a")
set(CMAKE_CXX_FLAGS_INIT "-march=armv8-a")
set(ENV{PKG_CONFIG_SYSROOT_DIR} "\${CMAKE_FIND_ROOT_PATH}")
set(ENV{PKG_CONFIG_LIBDIR}   "\${CMAKE_FIND_ROOT_PATH}/usr/lib/pkgconfig:\${CMAKE_FIND_ROOT_PATH}/usr/share/pkgconfig")
set(ENV{PKG_CONFIG_PATH}     "")
EOF

echo "== configure =="
cmake -S "$SRC_DIR" -B "$OUT/build" \
    -DCMAKE_TOOLCHAIN_FILE="$OUT/toolchain-aarch64.cmake" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SERVER=OFF -DBUILD_CLIENT=ON -DBUILD_TESTS=OFF \
    -DBUILD_WITH_ALSA=ON -DBUILD_WITH_PULSE=OFF -DBUILD_WITH_PIPEWIRE=OFF \
    -DBUILD_WITH_VORBIS=ON -DBUILD_WITH_FLAC=OFF -DBUILD_WITH_OPUS=OFF \
    -DBUILD_WITH_AVAHI=OFF -DBUILD_WITH_EXPAT=OFF -DBUILD_WITH_SSL=OFF \
    -DBUILD_WITH_SDL2=OFF -DBUILD_WITH_JACK=OFF \
    -DBOOST_ROOT="$XR_BOOST_ROOT" \
    -DBoost_INCLUDE_DIR="$BOOST_INC" \
    -DBoost_NO_BOOST_CMAKE=ON -DBoost_NO_SYSTEM_PATHS=ON -DBoost_USE_STATIC_LIBS=OFF

echo "== build =="
cmake --build "$OUT/build" --target snapclient -j "$(nproc)"

BIN="$OUT/build/bin/snapclient"
[ -f "$BIN" ] || BIN="$SRC_DIR/bin/snapclient"
cp "$BIN" "$OUT/snapclient"
"$TC/bin/aarch64-buildroot-linux-gnu-strip" "$OUT/snapclient"

echo
echo "== 产物 =="
ls -la "$OUT/snapclient"
echo "== 动态依赖 =="
"$TC/bin/aarch64-buildroot-linux-gnu-readelf" -d "$OUT/snapclient" | grep NEEDED
echo "== 需要的最高符号版本 =="
"$TC/bin/aarch64-buildroot-linux-gnu-readelf" -V "$OUT/snapclient" 2>/dev/null \
    | grep -oE "GLIBC_[0-9.]+|GLIBCXX_[0-9.]+" | sort -u -V | tail -4
echo
echo "板端启动(音频口=ALSA 直接出声, 会和前端抢 hw 设备):"
echo "  $OUT/snapclient tcp://<server>:1704 --soundcard <idx> --player alsa --mixer software"
echo "板端启动(音频口=FIFO, 不碰声卡, 推荐):"
echo "  $OUT/snapclient tcp://<server>:1704 --player=file:filename=/data/xiaorui/snapcast/snap.pcm"
