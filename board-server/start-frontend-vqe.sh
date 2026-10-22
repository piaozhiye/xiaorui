#!/bin/sh
# VQE 前端启动器(阶段3): arecord 走 rkaudio_vqe 插件 → FIFO → 前端(fifo: 采集).
# 进程隔离: sherpa(onnxruntime)与 rkaudio(MNN)同进程实测堆损坏, 分进程后各自稳定.
# 依赖: /etc/asound.conf 的 rkaudio_vqe_48k(阶段3已合并), 前端候选带 fifo: 支持.
# 用法(板上): sh /data/xiaorui-gateway/start-frontend-vqe.sh [候选二进制]
GW=/data/xiaorui-gateway
BIN="${1:-$GW/xiaorui_live-cand}"
FIFO=/tmp/vqe_mic.fifo
LOG=/tmp/xr_live.log
AREC_LOG=/tmp/vqe_arecord.log

# 杀旧(comm 精确匹配, 防 15 字符截断坑)
for p in /proc/[0-9]*; do
    c=$(cat "$p/comm" 2>/dev/null)
    case "$c" in
        xiaorui_live|xiaorui_live-*|arecord) kill "${p#/proc/}" 2>/dev/null ;;
    esac
done
sleep 2
rm -f "$FIFO"; mkfifo "$FIFO"

# 供给进程: VQE 16k/mono 原始流写 FIFO(打开阻塞到前端读端就绪)
cd /data/xiaorui-gateway
. /data/xiaorui-server/xiaorui.env
setsid arecord -D rkaudio_vqe_48k -r 16000 -c 1 -f S16_LE \
    --period-size=256 --buffer-size=4096 -t raw "$FIFO" > "$AREC_LOG" 2>&1 &
sleep 1

setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib \
    XR_MIC=fifo:$FIFO XR_KWS_TRUST_ACOUSTIC=1 "$BIN" > "$LOG" 2>&1 < /dev/null &

i=0
while [ $i -lt 20 ] && ! grep -q "聆听中" "$LOG" 2>/dev/null; do sleep 1; i=$((i+1)); done
if grep -q "聆听中" "$LOG" 2>/dev/null; then
    echo "vqe-frontend: ready (fifo=$FIFO)"
    exit 0
fi
echo "vqe-frontend: FAIL, 查 $LOG 与 $AREC_LOG"
exit 1
