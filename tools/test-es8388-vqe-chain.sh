#!/bin/bash
# ES8388+VQE 全链路验收 (docs/GLM-ES8388-MIC-REF-PLAN.md 阶段2/3 验收条款).
#
# 数值门槛(验收前声明, 不为全绿降标). rounds.txt 每行字段:
#   id|desc|wav|kws=N|kw_hit=N|submit=N|answered=N|selfwake=N|asr=文本
# 判定读第 4/5/6/7/8 列(2026-10-14 修正: 旧版读 3/4/5/6 列, G1/G4 判定完全失效).
#   G1 收尾余音不自唤醒: 回答排干→冷却窗内 KWS 唤醒数 = 0.
#      口径注意: 半双工架构播放期停采不喂 KWS, 本门只覆盖"播放线程收尾→回答完毕"
#      的余音窗; 播放中自唤醒与播报打断(全双工)不在本轮声明范围.
#   G2 命令送达云端并获回答: 5 轮中 ≥4 轮 submit≥1 且 answered=1 (云ASR模式判据,
#      不再用本地 RKNN 转写匹配当门槛——VQE 链本地转写染色属已知).
#   G3 零崩溃: 全程前端存活, 无 busy/xrun 风暴(≤2 次).
#   G4 唤醒率: ≥4/5 轮声学 KWS 命中(kws≥1).
#   G5 误唤醒负例: 3 段不含唤醒词语料播放期间 submit=0 (声学信任门的误唤醒回归).
#   (AEC 衰减与双讲近端保留已由 tools 侧 A/B 单独证明: 42.7dB/语音占比 0.43)
#
# 固定参数(板上启动器同源钉死): SAI loopback=5(4CH), ADC Data Select=Left Right
#   (numid=61=0), PGA=7(0-8档, numid=52/53), 原厂 2mic 配置, XR_MIC=xr_vqe_16k
#   XR_KWS_THRESHOLD=0.08.
# 流程: 推送候选+启动器 → 板上启动(杀残留/SAI 重挂/固定 mixer/记 PID) → 5 轮正向
#   → 3 轮负例 → 资源采样 → 按候选 PID 精确回收 → 恢复生产(USB, 带原 env)并校验.
# 用法: tools/test-es8388-vqe-chain.sh [输出目录]
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ADB="timeout 20 adb ${XZ_BOARD_SERIAL:+-s $XZ_BOARD_SERIAL}"
OUT="${1:-$REPO/test-results/vqe-chain-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
A=${XR_VQE_AUDIO_DIR:-$REPO/tests/audio}
LOG=/tmp/vqe_accept.log
PLAY_DEV=${PLAY_DEV:-plughw:1,0}
PLUGIN="$REPO/vendor/rkaudio-vqe/libasound_module_pcm_rkaudio_vqe.so"
PLUGIN_DIR=/data/xiaorui-gateway/vqe-candidate-alsa

# Cooperating test runs must not take over the same board concurrently.
exec 9>"/tmp/xiaozhi-vqe-accept-${XZ_BOARD_SERIAL:-}.lock"
flock -n 9 || { echo 'FAIL another VQE acceptance run holds the board lock'; exit 1; }

echo "[chain] out=$OUT"
CAND_STARTED=
MON=
cleanup() {
    result=$?
    trap - EXIT INT TERM
    if [ -n "$MON" ]; then kill "$MON" 2>/dev/null || true; wait "$MON" 2>/dev/null || true; fi
    if [ -n "$CAND_STARTED" ]; then
        # 只杀本次候选(板上启动器记录的 PID), 不再全局扫杀 xiaorui_live/arecord
        $ADB shell 'P=$(cat /tmp/vqe_cand.pid 2>/dev/null); E=$(readlink /proc/$P/exe 2>/dev/null); if [ "$E" = /data/xiaorui-gateway/xiaorui_live-cand ]; then kill -TERM "$P" 2>/dev/null; fi; rm -f /tmp/vqe_cand.pid' >/dev/null 2>&1
        sleep 2
        # 恢复生产: 同 start-all.sh 前端路径(含 xiaorui.env), 防双开
        $ADB shell 'if ! pgrep -x xiaorui_live >/dev/null; then cd /data/xiaorui-server || exit 1; . /data/xiaorui-server/xiaorui.env; setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib ./board-scripts/xiaorui_live > /tmp/xr_live.log 2>&1 < /dev/null & fi' >/dev/null 2>&1
        # 恢复校验: 30s 内看到"聆听中"才算恢复, 不再无凭据打印"生产已恢复"
        for i in $(seq 1 15); do
            if $ADB shell 'P=$(pgrep -x xiaorui_live); [ -n "$P" ] && [ "$(readlink /proc/$P/exe)" = /data/xiaorui-server/board-scripts/xiaorui_live ] && grep -q 聆听中 /tmp/xr_live.log' 2>/dev/null; then
                echo "[chain] 生产已恢复并校验(USB 麦)"; exit "$result"
            fi
            sleep 2
        done
        echo "[chain] 警告: 生产恢复 30s 内未确认, 人工查板上 /tmp/xr_live.log"
        exit 1
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Missing playback fixtures must fail before touching the active frontend.
for wav in wake2_time wake2_weather wake2_stop wake2_story wake2_name long_cook long_history cmd_joke; do
    [ -s "$A/$wav.wav" ] || { echo "FAIL missing fixture $wav"; exit 1; }
done
[ -s "$PLUGIN" ] || { echo 'FAIL missing candidate plugin'; exit 1; }

# ---- 1. 部署候选+启动器(自含: 不依赖板上已有版本) ----
$ADB push "$REPO/board/xiaorui_live" /data/xiaorui-gateway/xiaorui_live-cand >/dev/null || exit 1
$ADB push "$REPO/board-server/start-vqe-accept.sh" /data/xiaorui-gateway/start-vqe-accept.sh >/dev/null || exit 1
$ADB shell "mkdir -p $PLUGIN_DIR && chmod +x /data/xiaorui-gateway/xiaorui_live-cand" || exit 1
$ADB push "$PLUGIN" "$PLUGIN_DIR/libasound_module_pcm_rkaudio_vqe.so" >/dev/null || exit 1
sha256sum "$PLUGIN" > "$OUT/plugin-local.sha256"
$ADB shell "sha256sum $PLUGIN_DIR/libasound_module_pcm_rkaudio_vqe.so" | tr -d '\r' > "$OUT/plugin-board.sha256" || exit 1
[ "$(cut -d' ' -f1 "$OUT/plugin-local.sha256")" = "$(cut -d' ' -f1 "$OUT/plugin-board.sha256")" ] || exit 1

# ---- 2. 启动候选(板上启动器: 杀残留→SAI 重挂→固定 mixer→起候选→就绪) ----
CAND_STARTED=1
timeout 75 adb ${XZ_BOARD_SERIAL:+-s $XZ_BOARD_SERIAL} shell "sh /data/xiaorui-gateway/start-vqe-accept.sh" > "$OUT/startup.log" 2>&1 || { echo "[chain] FAIL 候选启动失败"; exit 1; }
grep -qx READY "$OUT/startup.log" || { echo "[chain] FAIL 候选未就绪"; exit 1; }
P=$($ADB shell 'cat /tmp/vqe_cand.pid' | tr -d '\r ')
$ADB shell "cat /proc/$P/maps" > "$OUT/candidate-maps.txt" || exit 1
grep -q "$PLUGIN_DIR/libasound_module_pcm_rkaudio_vqe.so" "$OUT/candidate-maps.txt" || { echo 'FAIL private plugin not loaded'; exit 1; }
echo "[chain] 候选就绪(VQE 链)"
sleep 8  # Allow the first capture/ALSA buffers to settle before acoustic playback.

# 资源采样
( for k in $(seq 1 40); do
    $ADB shell "date -u +%T; echo \"pid=$P rss=\$(awk '/VmRSS/{print \$2}' /proc/$P/status) fd=\$(ls /proc/$P/fd 2>/dev/null | wc -l)\"" >> "$OUT/resource.txt" 2>&1
    sleep 15
  done ) &
MON=$!

# ---- 3. 五轮正向验收(间隙语料: 唤醒词 +3.0s 静音 + 指令;
# 单口气语料(wake_tts_*)的指令会被纯唤醒 ack 冷却+回声护栏吞掉, 2026-10-15 实测 0/5) ----
declare -a ROUNDS=(
    "R1 唤醒+普通指令 wake2_time    几点 cmd_time.wav"
    "R2 唤醒+天气     wake2_weather 天气 cmd_weather.wav"
    "R3 唤醒+两字短句 wake2_stop    停下 short_stop.wav"
    "R4 唤醒+长回答   wake2_story   睡前故事 long_story.wav"
    "R5 再唤醒+指令   wake2_name    名字 cmd_name.wav"
)
> "$OUT/rounds.txt"
for r in "${ROUNDS[@]}"; do
    set -- $r; id=$1; desc=$2; wav=$3; kw=$4; command_wav=$5
    OFF=$($ADB shell "wc -c <$LOG" | tr -d '\r ')
    aplay -D "$PLAY_DEV" -q "$A/$wav.wav" || exit 1
    # 等回答完毕(长回答最多 150s)
    ok=0
    for i in $(seq 1 150); do
        if $ADB shell "tail -c +$((OFF+1)) $LOG 2>/dev/null" | grep -q "回答完毕"; then ok=1; break; fi
        sleep 1
    done
    sleep 8  # Include the post-answer echo window in the archived evidence.
    SLICE=$($ADB shell "tail -c +$((OFF+1)) $LOG" | grep -vE '^LOG')
    printf '%s\n' "$SLICE" > "$OUT/$id.log"
    # Use the command component's declared transcript, not wakeup/completion alone.
    if ! python3 "$REPO/tools/score-acceptance-turn.py" --manifest "$A/manifest.json" \
        --wav "$command_wav" --id "$id" < "$OUT/$id.log" > "$OUT/$id-score.json"; then
        ok=0
    fi
    kws=$(echo "$SLICE" | grep -c "KWS 命中")
    asr=$(echo "$SLICE" | grep "ASR 精修" | tail -1 | sed 's/.*RKNN): //;s/ (.*//')
    casr=$(echo "$SLICE" | grep "ASR 回执" | tail -1 | sed 's/.*回执: //')
    submit=$(echo "$SLICE" | grep -cE "submitted")
    selfwake=$(echo "$SLICE" | sed -n '/播放线程收尾/,$p' | grep -c "KWS 命中")
    kw_hit=$(echo "$asr $casr" | grep -c "$kw")
    echo "$id|$desc|$wav|kws=$kws|kw_hit=$kw_hit|submit=$submit|answered=$ok|selfwake=$selfwake|asr=$asr 云:$casr" >> "$OUT/rounds.txt"
    echo "[chain] $id: kws=$kws kw=$kw_hit sub=$submit ans=$ok selfwake=$selfwake asr=$asr 云:$casr"
done

# ---- 3b. 误唤醒负例(声学信任门回归): 不含唤醒词语料, 不得有提交 ----
> "$OUT/negatives.txt"
declare -a NEGS=("long_cook" "long_history" "cmd_joke")
NEG_SUBMIT_TOT=0
for wav in "${NEGS[@]}"; do
    # Ensure follow-up listening has expired before evaluating a cold negative.
    sleep 35
    OFF=$($ADB shell "wc -c <$LOG" | tr -d '\r ')
    aplay -D "$PLAY_DEV" -q "$A/$wav.wav" || exit 1
    sleep 12   # 语料较长, 等说完+判定落定
    SLICE=$($ADB shell "tail -c +$((OFF+1)) $LOG" | grep -vE '^LOG')
    kws=$(echo "$SLICE" | grep -c "KWS 命中")
    submit=$(echo "$SLICE" | grep -cE "submitted")
    ans=$(echo "$SLICE" | grep -c "回答完毕")
    NEG_SUBMIT_TOT=$((NEG_SUBMIT_TOT + submit))
    echo "NEG|$wav|kws=$kws|submit=$submit|answered=$ans" >> "$OUT/negatives.txt"
    echo "[chain] 负例 $wav: kws=$kws submit=$submit answered=$ans"
    sleep 5
done

kill $MON 2>/dev/null
wait "$MON" 2>/dev/null || true
MON=

# ---- 4. 判定(列号: 4=kws 5=kw_hit 6=submit 7=answered 8=selfwake) ----
ALIVE=0
$ADB shell "[ \"\$(readlink /proc/$P/exe)\" = /data/xiaorui-gateway/xiaorui_live-cand ]" && ALIVE=1
BUSY=$($ADB shell "grep -ciE 'busy|xrun|overrun|underrun' $LOG" | tr -d '\r ')
BAD_OUTPUT=$($ADB shell "grep -cE 'PAD-DIRTY|invalid output bytes|CRASH' $LOG" | tr -d '\r ')
WAKE_OK=$(awk -F'|' '{gsub("kws=","",$4)} $4+0>0' "$OUT/rounds.txt" | wc -l)
KW_OK=$(awk -F'|' '{gsub("kw_hit=","",$5)} $5+0>0' "$OUT/rounds.txt" | wc -l)
SENT_OK=$(awk -F'|' '{gsub("submit=","",$6); gsub("answered=","",$7)} $6+0>0 && $7=="1"' "$OUT/rounds.txt" | wc -l)
ANS_OK=$(awk -F'|' '{gsub("answered=","",$7)} $7=="1"' "$OUT/rounds.txt" | wc -l)
SELFWAKE_TOT=$(awk -F'|' '{gsub("selfwake=","",$8); s+=$8} END{print s}' "$OUT/rounds.txt")
{
echo "G1 收尾余音不自唤醒(口径:收尾+8秒,播放期停采): selfwake_total=$SELFWAKE_TOT answered=$ANS_OK (门槛=0且至少一轮有效回答) $([ "$SELFWAKE_TOT" = "0" ] && [ "$ANS_OK" -gt 0 ] && echo PASS || echo FAIL)"
echo "G2 命令送达云端并获回答: submit+answered=$SENT_OK/5 (门槛≥4) $([ "$SENT_OK" -ge 4 ] && echo PASS || echo FAIL)"
echo "G3 零崩溃: alive=$ALIVE busy_xrun=$BUSY bad_output=$BAD_OUTPUT (门槛 alive=1,busy_xrun≤2,bad_output=0) $([ "$ALIVE" = "1" ] && [ "$BUSY" -le 2 ] && [ "$BAD_OUTPUT" = "0" ] && echo PASS || echo FAIL)"
echo "G4 唤醒率: $WAKE_OK/5 (门槛≥4) $([ "$WAKE_OK" -ge 4 ] && echo PASS || echo FAIL)"
echo "G5 误唤醒负例: submit_total=$NEG_SUBMIT_TOT (门槛=0) $([ "$NEG_SUBMIT_TOT" = "0" ] && echo PASS || echo FAIL)"
echo "辅助: answered=$ANS_OK/5 kw文本命中=$KW_OK/5 (云回执+本地转写并计, 仅参考)"
} | tee "$OUT/verdict.txt"
PASSN=$(grep -c PASS "$OUT/verdict.txt")
echo "[chain] RESULT $PASSN/5 $([ "$PASSN" = "5" ] && echo ALL-PASS || echo HAS-FAIL)"
[ "$PASSN" = "5" ]
