#!/bin/bash
# ES8388 阶段1: 4通道诊断与通道映射 (按 docs/GLM-ES8388-MIC-REF-PLAN.md 阶段1).
# 只录 raw hw:rockchipes8388,0 4ch/48k, 不接 ASR/AEC/VQE. 四组场景:
#   R1 静音底噪 / R2 只播放(分左/右/双异频音, 辨 ref-L/ref-R) /
#   R3 近端语音(板端不播放) / R4 板端播放+近端语音(双讲)
# 前提: mixer 'SAI1 Software Digital Loopback Mode'=5 (4CH: 2 Mics + 2 Loopbacks, 用户已设).
# 前端停期间录音(ZR 麦会旁听板内播放污染轮次), EXIT/INT/TERM 恢复.
# 用法: tools/test-es8388-mic-ref.sh [-s serial] [输出目录]
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
while [ $# -gt 0 ]; do case "$1" in -s) SERIAL="$2"; shift 2;; esac; done
ADB="adb ${SERIAL:+-s $SERIAL}"
OUT="${1:-$REPO/test-results/es8388-map-$(date +%Y%m%d-%H%M%S)}"
[ -d "$OUT" ] || { OUT="$REPO/test-results/es8388-map-$(date +%Y%m%d-%H%M%S)"; }
mkdir -p "$OUT"
CARD=rockchipes8388
MIXCTRL='SAI1 Software Digital Loopback Mode'
OB=/data/micref-test
echo "[map] out=$OUT"

cleanup() {
    if [ -n "${FE_PID:-}" ] && ! $ADB shell "test -d /proc/$FE_PID" 2>/dev/null; then
        $ADB shell "cd /data/xiaorui-server && setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib /data/xiaorui-server/board-scripts/xiaorui_live > /tmp/xr_live.log 2>&1 < /dev/null &" \
            && echo "[map] 前端已恢复"
    fi
}
trap cleanup EXIT INT TERM

# 前置校验: mixer 必须是 5
V=$($ADB shell "amixer -c $CARD cget name='$MIXCTRL' | grep ': values'" | tr -d '\r' | awk -F= '{print $NF}')
[ "$V" = "5" ] || { echo "[map] FAIL mixer=$V (要求 5: 4CH 2Mics+2Loopbacks)"; exit 1; }

# 停前端(精确 PID)
FE_PID=$($ADB shell 'for p in /proc/[0-9]*; do c=$(cat $p/comm 2>/dev/null); case "$c" in xiaorui_live|xiaorui_live-*) echo ${p#/proc/};; esac; done | head -1' | tr -d '\r ')
[ -n "$FE_PID" ] && $ADB shell "kill $FE_PID" && echo "[map] 前端($FE_PID)已停" && sleep 2
$ADB shell "mkdir -p $OB"

SP=$REPO/tests/audio/cmd_time.wav   # 近端语音(PC 喇叭放, 16k mono → aplay 经 plug 转 48k)
for f in /data/tone_L1k.wav /data/tone_R800.wav /data/tone_LR.wav; do
    $ADB shell "[ -f $f ]" || { echo "[map] 缺测试音 $f"; exit 1; }
done

rec() { $ADB shell "arecord -D hw:$CARD,0 -r 48000 -c 4 -f S16_LE -d $2 $OB/$1 2>&1" | tail -1; }

echo "[map] R1 静音底噪 8s"; rec r1_silence.wav 8
echo "[map] R2 只播放 12s: 2s 起 L-only(1k) → R-only(800) → 双异频(1k/1.5k)"
( sleep 2
  $ADB shell "aplay -D hw:$CARD,0 /data/tone_L1k.wav" >/dev/null 2>&1
  sleep 1
  $ADB shell "aplay -D hw:$CARD,0 /data/tone_R800.wav" >/dev/null 2>&1
  sleep 1
  $ADB shell "aplay -D hw:$CARD,0 /data/tone_LR.wav" >/dev/null 2>&1 ) &
rec r2_play.wav 12
wait
echo "[map] R3 近端语音 10s (板端不播放)"
( sleep 2; aplay -q "$SP"; sleep 0.5; aplay -q "$SP"; sleep 0.5; aplay -q "$SP" ) &
rec r3_nearend.wav 10
wait
echo "[map] R4 双讲 12s: 板播放 L 音 + 近端语音交替"
( sleep 2
  $ADB shell "aplay -D hw:$CARD,0 /data/tone_L1k.wav >/dev/null 2>&1 &"
  aplay -q "$SP"; sleep 0.5; aplay -q "$SP"
  $ADB shell "aplay -D hw:$CARD,0 /data/tone_LR.wav" >/dev/null 2>&1 ) &
rec r4_doubletalk.wav 12
wait

$ADB shell "ls -la $OB/" | tee "$OUT/list.txt"
for f in $($ADB shell "ls $OB" | tr -d '\r'); do $ADB pull "$OB/$f" "$OUT/" >/dev/null; done
echo "[map] 完成: $OUT → python3 tools/analyze-mic-ref.py $OUT"
