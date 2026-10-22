#!/bin/sh
# VQE 验收链启动器(板上): 杀残留 → 固定 mixer(按卡名, 不写死卡号) → 起候选 → 记 PID → 就绪判定.
# 由 tools/test-es8388-vqe-chain.sh 推送并调用; 收尾由链脚本按 /tmp/vqe_cand.pid
# 精确回收(不再全局扫杀 xiaorui_live/arecord).
# 固定参数与链脚本声明一致: SAI loopback=5(4CH), ADC Data Select=Left Right(numid=61=0),
# PGA=7(0-8 档, numid=52/53), XR_MIC=xr_vqe_16k XR_KWS_THRESHOLD=0.08.
GW=/data/xiaorui-gateway
LOG=/tmp/vqe_accept.log
CARD=rockchipes8388
PLUGIN_DIR=$GW/vqe-candidate-alsa
PRODUCTION=/data/xiaorui-server/board-scripts/xiaorui_live
CANDIDATE=$GW/xiaorui_live-cand
set -eu

# Validate candidate inputs before stopping the active frontend.
[ -x "$CANDIDATE" ] || { echo 'FAIL missing candidate'; exit 1; }
[ -s "$PLUGIN_DIR/libasound_module_pcm_rkaudio_vqe.so" ] || {
    echo 'FAIL missing private VQE plugin'; exit 1;
}
[ -r /data/xiaorui-server/xiaorui.env ] || exit 1

for p in /proc/[0-9]*; do
    exe=$(readlink "$p/exe" 2>/dev/null || true)
    case "$exe" in
        "$PRODUCTION"|"$CANDIDATE") kill -TERM "${p#/proc/}" 2>/dev/null || true ;;
    esac
done
sleep 2
for p in /proc/[0-9]*; do
    exe=$(readlink "$p/exe" 2>/dev/null || true)
    case "$exe" in
        "$PRODUCTION"|"$CANDIDATE")
            echo "FAIL frontend still running pid=${p#/proc/}"; exit 1 ;;
    esac
done

# SAI 0→5 重挂(杀进程后偶发 EINVAL/EPERM 瞬态, 实测重挂即愈)
amixer -c "$CARD" cset name='SAI1 Software Digital Loopback Mode' 0 >/dev/null; sleep 1
amixer -c "$CARD" cset name='SAI1 Software Digital Loopback Mode' 5 >/dev/null; sleep 1
amixer -c "$CARD" cset numid=61 0 >/dev/null     # ADC Data Select = Left Right
amixer -c "$CARD" cset numid=52 7 >/dev/null     # PGA Left
amixer -c "$CARD" cset numid=53 7 >/dev/null     # PGA Right

cd $GW || exit 1
. /data/xiaorui-server/xiaorui.env
XR_MIC=xr_vqe_16k XR_KWS_THRESHOLD=0.08 XR_FOLLOW_WINDOW_S=30 XR_TRACE_DECISION=1 setsid env ALSA_PLUGIN_DIR="$PLUGIN_DIR" LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib \
    $GW/xiaorui_live-cand > $LOG 2>&1 < /dev/null &
CAND_PID=$!
echo "$CAND_PID" > /tmp/vqe_cand.pid

candidate_ready() {
    kill -0 "$CAND_PID" 2>/dev/null &&
        grep -q '聆听中' "$LOG" && grep -q '\[xr\] ambient=' "$LOG"
}

i=0
while [ $i -lt 25 ] && ! candidate_ready; do
    kill -0 "$CAND_PID" 2>/dev/null || { echo NOT-READY; exit 1; }
    sleep 2; i=$((i+1))
done
if candidate_ready; then
    echo READY
else
    echo NOT-READY; exit 1
fi
