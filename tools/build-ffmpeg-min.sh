#!/bin/bash
# 最小化交叉构建 ffmpeg：仅 http/https 拉流 + mp3/aac/flac/opus/PCM WAV 解码 + swresample。
# 产物约 2M/个，动态链接板上系统库（libssl/libcrypto/libc），DNS 走系统 NSS。
# 用途：网关 pcm_frames() 的解码子进程（见 README §3.1/3.2）。
#
# 在任意装有 RK3576 Linux SDK（或等价 aarch64 buildroot 工具链+sysroot）的主机上运行；
# 产物在 $WORK/src/ffmpeg 与 src/ffprobe，由 tools/install-board-music-runtime.sh 推板。
# 前置：源码包 ffmpeg-7.1.1.tar.xz 放在 $WORK（ffmpeg.org 下载可能截断，curl -C - 续传）。
#
# 可用环境变量覆盖（默认值适配 SDK 默认安装布局）：
#   SDK_ROOT  SDK 根目录            （默认 ~/RK3576_LINUX6.1_SDK）
#   CROSS     交叉前缀               （默认 $SDK_ROOT 下 gcc-arm-10.3* 自动发现）
#   STAGING   buildroot sysroot      （默认 $SDK_ROOT/buildroot/output/*/staging 第一个命中）
#   WORK      工作目录               （默认 ~/ffmpeg-min）
#
# 两个坑（2026-11-02 实测）：
#   1. -f s16le 对应的 configure 组件名是 pcm_s16le（CLI 格式名 != 组件名），
#      配错不报错、运行时才炸 "output format 's16le' is not known"。
#   2. sysroot 用任一已构建配置的 staging 均可（glibc 2.38，板上 2.41 向后兼容）；
#      openssl 的 OPENSSLDIR=/etc/ssl 与板上 CA 布局一致，https 默认证书验证直接可用。
set -e
SDK=${SDK_ROOT:-$HOME/RK3576_LINUX6.1_SDK}
[ -d "$SDK" ] || { echo "SDK 不存在: $SDK（用 SDK_ROOT=... 指定）" >&2; exit 1; }

if [ -z "${CROSS:-}" ]; then
  # CROSS 是前缀串(结尾带-)，不是文件名：glob 到 gcc 二进制再剥掉尾部的 "gcc"
  GCC_BIN=$(ls "$SDK"/prebuilts/gcc/linux-x86/aarch64/gcc-arm-10.3*/bin/aarch64-none-linux-gnu-gcc 2>/dev/null | head -1)
  [ -n "$GCC_BIN" ] && CROSS="${GCC_BIN%gcc}"
fi
[ -n "${CROSS:-}" ] || { echo "未找到 aarch64 交叉前缀（用 CROSS=... 指定）" >&2; exit 1; }

if [ -z "${STAGING:-}" ]; then
  STAGING=$(ls -d "$SDK"/buildroot/output/*/staging 2>/dev/null | head -1)
fi
[ -n "$STAGING" ] || { echo "未找到 buildroot staging sysroot（先构建一次 buildroot，或 STAGING=... 指定）" >&2; exit 1; }

W=${WORK:-$HOME/ffmpeg-min}
mkdir -p "$W"

# The existing gateway build keeps HTTPS support by default. A board-local
# Snapcast controller only reads the local music library, so it can build an
# isolated no-TLS decoder to avoid binding to a particular OpenSSL ABI.
WITH_OPENSSL=${XR_FFMPEG_ENABLE_OPENSSL:-1}
TARGET_CPU=${XR_FFMPEG_CPU:-cortex-a53}
case "$WITH_OPENSSL" in
  1) OPENSSL_CONFIG=--enable-openssl; PROTOCOLS=file,http,https,tcp,tls,pipe ;;
  0) OPENSSL_CONFIG=--disable-openssl; PROTOCOLS=file,http,tcp,pipe ;;
  *) echo "XR_FFMPEG_ENABLE_OPENSSL must be 0 or 1" >&2; exit 2 ;;
esac

# 源码自动获取: 缺失或指纹不符时从 ffmpeg.org 断点续传下载(国内链路常见截断,
# -C - 续传可救, 最多 5 次)。sha256 是 2026-11-02 自 ffmpeg.org 取得、经上板
# 验证构建的同一份文件 —— 锁指纹保证 runbook 复现的是验证过的源码。
# 若本地已是"完整但损坏"的文件, 续传会 416 耗尽重试, 按提示删了重下。
TARBALL=ffmpeg-7.1.1.tar.xz
SHA256=733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1
if ! (cd "$W" && echo "$SHA256  $TARBALL" | sha256sum -c --quiet 2>/dev/null); then
  echo "获取 $TARBALL (ffmpeg.org, 断点续传)"
  ok=0
  for i in 1 2 3 4 5; do
    curl -fsSL -C - -o "$W/$TARBALL" "https://ffmpeg.org/releases/$TARBALL" && ok=1 && break
    echo "  第 $i 次尝试失败, 3s 后续传重试" >&2
    sleep 3
  done
  [ "$ok" = 1 ] || { echo "下载失败; 若本地文件完整但损坏, 删 $W/$TARBALL 重试" >&2; exit 1; }
  (cd "$W" && echo "$SHA256  $TARBALL" | sha256sum -c) || exit 1
fi

echo "SDK=$SDK"
echo "CROSS=$CROSS"
echo "STAGING=$STAGING"
echo "WORK=$W"

cd "$W"
rm -rf src installed
mkdir -p src
tar xf ffmpeg-7.1.1.tar.xz -C src --strip-components=1
cd src

./configure \
  --cross-prefix="$CROSS" \
  --arch=aarch64 --target-os=linux --enable-cross-compile \
  --cpu="$TARGET_CPU" \
  --sysroot="$STAGING" \
  --prefix="$W/installed" \
  --disable-autodetect --disable-doc --disable-debug --disable-ffplay \
  --disable-everything --enable-small \
  "$OPENSSL_CONFIG" \
  --enable-protocol="$PROTOCOLS" \
  --enable-demuxer=aac,flac,hls,mp3,mov,ogg,wav \
  --enable-decoder=aac,flac,mp3,mp3float,opus,pcm_s16le \
  --enable-parser=aac,flac,mp3,opus \
  --enable-filter=aformat,aresample,anull \
  --enable-muxer=pcm_s16le,null \
  --enable-encoder=pcm_s16le

echo ===CONFIG-OK===
make -j"$(nproc)" ffmpeg ffprobe
echo ===MAKE-OK===
ls -lh ffmpeg ffprobe
file ffmpeg 2>/dev/null | head -1
"${CROSS}readelf" -d ffmpeg | grep NEEDED
