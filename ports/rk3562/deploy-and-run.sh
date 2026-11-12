#!/bin/bash
# PC 侧: 把 xiaorui 前端部署到 RK3562 板子并拉起来。
#
# 前置:
#   - adb 已连上板子(唯一一台 RK3562 EVB2, serial 见下)
#   - deploy-vqe.sh 已生成板上 ALSA 配置
#   - deploy-board-gateway.sh 已启动板内网关
#
# 用法: ports/rk3562/deploy-and-run.sh [serial]
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
SHERPA_LIB_DIR="${XR_SHERPA_LIB_DIR:-$REPO/vendor/sherpa-onnx/lib}"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
SERIAL="${1:-}"
if [ -z "$SERIAL" ]; then
    SERIAL=$(adb devices | awk '/device$/{print $1}' | head -1)
fi
[ -n "$SERIAL" ] || { echo "没有在线的 adb 设备" >&2; exit 2; }

echo "== 板子身份确认 =="
MODEL=$(adb -s "$SERIAL" shell 'cat /proc/device-tree/model' 2>/dev/null | tr -d '\0')
echo "  $SERIAL -> $MODEL"
case "$MODEL" in
    *RK3562*) : ;;
    *) echo "  !! 不是 RK3562 ($MODEL)，停止" >&2; exit 1 ;;
esac

for library in libsherpa-onnx-c-api.so libonnxruntime.so; do
    [ -f "$SHERPA_LIB_DIR/$library" ] || {
        echo "缺运行时库: $SHERPA_LIB_DIR/$library，请设置 XR_SHERPA_LIB_DIR" >&2
        exit 2
    }
done
adb -s "$SERIAL" shell "/data/gwvenv/bin/python - <<'PY'
import ctypes

alsa = ctypes.CDLL('libasound.so.2')
alsa.snd_config_update.restype = ctypes.c_int
if alsa.snd_config_update() < 0:
    raise SystemExit('ALSA 配置解析失败，请先运行 deploy-vqe.sh')
alsa.snd_config_search.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                 ctypes.POINTER(ctypes.c_void_p)]
alsa.snd_config_search.restype = ctypes.c_int
root = ctypes.c_void_p.in_dll(alsa, 'snd_config')
for name in ('xr_vqe_16k', 'xr_spk'):
    node = ctypes.c_void_p()
    if alsa.snd_config_search(root, ('pcm.' + name).encode(), ctypes.byref(node)) < 0:
        raise SystemExit('缺 ALSA 设备 ' + name + '，请先运行 deploy-vqe.sh')
PY"

echo "== 推送前端二进制 =="
adb -s "$SERIAL" shell 'mkdir -p /data/xiaorui/{bin,lib,assets/kws}' >/dev/null
adb -s "$SERIAL" push "$REPO/board/xiaorui_live" /data/xiaorui/bin/ >/dev/null

echo "== 推送运行时库 (sherpa-onnx C API + onnxruntime) =="
adb -s "$SERIAL" push "$SHERPA_LIB_DIR/libsherpa-onnx-c-api.so" /data/xiaorui/lib/ >/dev/null
adb -s "$SERIAL" push "$SHERPA_LIB_DIR/libonnxruntime.so" /data/xiaorui/lib/ >/dev/null

echo "== 推送 KWS 模型 =="
adb -s "$SERIAL" push "$REPO/models/model-kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" /data/xiaorui/assets/kws/ >/dev/null
adb -s "$SERIAL" push "$REPO/models/model-kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx" /data/xiaorui/assets/kws/ >/dev/null
adb -s "$SERIAL" push "$REPO/models/model-kws/joiner-epoch-12-avg-2-chunk-16-left-64.onnx" /data/xiaorui/assets/kws/ >/dev/null
adb -s "$SERIAL" push "$REPO/models/model-kws/tokens.txt" /data/xiaorui/assets/kws/ >/dev/null
adb -s "$SERIAL" push "$REPO/keywords.txt" /data/xiaorui/assets/kws/ >/dev/null

echo "== 推送提示音(保留已有 ALSA 配置) =="
adb -s "$SERIAL" shell 'mkdir -p /data/xiaorui-server/board-scripts' >/dev/null
for c in xr_ack24k xr_hmm24k xr_fail24k xr_volack24k; do
    adb -s "$SERIAL" push "$REPO/board-server/cues/$c.wav" /data/xiaorui-server/board-scripts/ >/dev/null
done
adb -s "$SERIAL" shell 'mkdir -p /data/xiaorui-server; echo 100 > /data/xiaorui-server/xiaorui_volume'

echo "== 推送 env + 启动脚本 =="
sed -e "s|^export XR_DEVICE_ID=.*|export XR_DEVICE_ID=xiaorui-rk3562|" \
    "$DIR/frontend.env.example" > "$TMP_DIR/frontend.env"
adb -s "$SERIAL" push "$TMP_DIR/frontend.env" /data/xiaorui/xiaorui.env >/dev/null
adb -s "$SERIAL" push "$DIR/start-frontend.sh" /data/xiaorui/start-frontend.sh >/dev/null
adb -s "$SERIAL" shell 'chmod +x /data/xiaorui/start-frontend.sh'
# 时间脚本(时区 + 无网时的 RTC/last-good-epoch 回退)
adb -s "$SERIAL" push "$DIR/board-time.sh" /data/xiaorui/board-time.sh >/dev/null
adb -s "$SERIAL" shell 'chmod +x /data/xiaorui/board-time.sh'
# 开机自启 (buildroot busybox init 会跑 /etc/init.d/S*)
adb -s "$SERIAL" push "$DIR/init-S98xiaorui" /etc/init.d/S98xiaorui >/dev/null
adb -s "$SERIAL" shell 'chmod +x /etc/init.d/S98xiaorui'

echo "== 启动前端 =="
adb -s "$SERIAL" shell 'pkill -x xiaorui_live 2>/dev/null; sleep 1;
    sh /etc/init.d/S98xiaorui start'
sleep 6
adb -s "$SERIAL" shell 'tail -20 /data/xiaorui/frontend.log'
