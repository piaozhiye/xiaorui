#!/bin/bash
# 在正式网关/前端安装前，stage Python + RKLLM runtime/model 并验证 ABI。
# 只放入推理冒烟所需的运行时文件，不安装 ASR/TTS/网关、不起常驻服务。
#
# 用法: bash ports/rk3562-es8388/stage-runtime.sh SERIAL
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
SERIAL="${1:-}"
SRV=/data/xiaorui-server
PY=/data/miniforge/bin/python3.14
MODEL="$SRV/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm"
MINIFORGE="${XR_MINIFORGE:-$REPO/board-server/miniforge.sh}"
RKLLM="${XR_RKLLM_MODEL:-$REPO/models/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm}"
LIBGOMP="${XR_LIBGOMP:-}"
[ -n "$LIBGOMP" ] || { echo "请设置 XR_LIBGOMP 指向 aarch64 的 libgomp.so.1" >&2; exit 2; }
GLIBC_OVERRIDE="${XR_MINIFORGE_GLIBC_OVERRIDE:-2.41}"

[ -n "$SERIAL" ] || { echo "用法: $0 SERIAL" >&2; exit 2; }
[[ "$GLIBC_OVERRIDE" =~ ^[0-9]+\.[0-9]+$ ]] \
    || { echo "无效 XR_MINIFORGE_GLIBC_OVERRIDE: $GLIBC_OVERRIDE" >&2; exit 2; }
ADB="adb -s $SERIAL"

die() { echo "!! $*" >&2; exit 1; }

MODEL_NAME=$($ADB shell 'cat /proc/device-tree/model' 2>/dev/null | tr -d '\0')
case "$MODEL_NAME" in
    *RK3562*) : ;;
    *) die "不是 RK3562 ($MODEL_NAME)" ;;
esac

for f in "$MINIFORGE" "$REPO/board-server/librkllmrt.so" \
         "$REPO/board-server/llm_rkllm.py" "$RKLLM" "$LIBGOMP"; do
    [ -f "$f" ] || die "缺运行时资源: $f"
done

echo "== Python 运行时 =="
if ! $ADB shell "test -x $PY" 2>/dev/null; then
    # 不覆盖未知/不完整的 Python 目录。首次安装才解自包含的本地 Miniforge 包。
    if $ADB shell 'test -e /data/miniforge' 2>/dev/null; then
        die "/data/miniforge 已存在但没有可运行的 Python；检查并人工修复，不覆盖"
    fi
    $ADB push "$MINIFORGE" /tmp/xr-es8388-miniforge.sh >/dev/null
    $ADB shell "cd /data && CONDA_OVERRIDE_GLIBC=$GLIBC_OVERRIDE sh /tmp/xr-es8388-miniforge.sh -b -p /data/miniforge"
    $ADB shell 'rm -f /tmp/xr-es8388-miniforge.sh'
fi
PYVER=$($ADB shell "$PY -V" 2>/dev/null | tr -d '\0')
case "$PYVER" in
    "Python 3.14"*) echo "Python ABI: $PYVER" ;;
    *) die "不支持的板端 Python: ${PYVER:-无输出}" ;;
esac

echo "== stage RKLLM 运行时、libgomp 与模型 (不启动服务) =="
$ADB shell "mkdir -p $SRV/lib"
$ADB push "$REPO/board-server/librkllmrt.so" "$SRV/" >/dev/null
$ADB push "$REPO/board-server/llm_rkllm.py" "$SRV/" >/dev/null
$ADB push "$RKLLM" "$MODEL" >/dev/null
$ADB push "$LIBGOMP" "$SRV/lib/libgomp.so.1" >/dev/null

LOAD=$($ADB shell "LD_LIBRARY_PATH=$SRV/lib $PY -c \"import ctypes; ctypes.CDLL('$SRV/librkllmrt.so'); print('loaded')\"" \
    2>/dev/null | tr -d '\0' | tail -1)
[ "$LOAD" = "loaded" ] || die "librkllmrt.so 无法加载: ${LOAD:-无输出}"
echo "RKLLM ABI: librkllmrt.so + libgomp.so.1 加载成功"
echo "运行时文件已 stage 到 $SRV (未启动服务)"
