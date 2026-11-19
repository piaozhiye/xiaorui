#!/bin/bash
# RK3562 板端声学验收: PC 放音 -> 板子拾音 -> 唤醒 -> ASR -> LLM -> TTS -> 板子出声。
#
# 与 tools/run-board-acceptance.sh 的区别: 那个是"板内栈 vs 云端"的对比验收,
# 这个只测**本地全链路**的语音回路, 并把每一条的结果(唤醒/识别/合成/回答/
# 端到端时延)收集成报告, 用来定位"哪一环掉了"。
#
# 用法:
#   bash tools/run-acoustic-suite.sh [-s serial] [-o outdir] [-r rounds] [--gain G]
#
# 关键点(踩过的坑):
#   - 板子只有 2GB: 跑之前先确认内存够(NPU 模型 + 网关 + 前端常驻约 1.7GB),
#     余量不足就 OOM 黑屏。
#   - 唤醒词素材要够响: PDM 麦 + 6mic 波束后电平低(实测到板只有 -33dBFS),
#     用 --gain 给素材加数字增益再放(默认 6dB)。
#   - 板子麦克风在喇叭旁边, PC 放音 -> 板子拾音 -> 板子喇叭播回答,
#     所以**别同时**在 PC 上放别的声。
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
OUT="${XR_ACOUSTIC_OUT:-$REPO/test-results/acoustic-$(date +%Y%m%d-%H%M%S)}"
ROUNDS=1
GAIN=6
PLAY_DEV="${PLAY_DEV:-plughw:1,0}"

while [ $# -gt 0 ]; do
    case "$1" in
        -s) SERIAL="$2"; shift 2 ;;
        -o) OUT="$2"; shift 2 ;;
        -r) ROUNDS="$2"; shift 2 ;;
        --gain) GAIN="$2"; shift 2 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

ADB="adb ${SERIAL:+-s $SERIAL}"
[ -n "$SERIAL" ] || SERIAL=$($ADB devices | awk '/device[[:space:]]+device$/{print $1; exit}')
[ -n "$SERIAL" ] || { echo "没有在线 adb 设备" >&2; exit 2; }
ADB="adb -s $SERIAL"
mkdir -p "$OUT"

GWLOG=/data/xiaorui-gateway/data/gateway.log
FELOG=/data/xiaorui/frontend.log

note() { echo "[suite] $*"; }

# ---------- 0) 前置检查 ----------
note "板子: $SERIAL"
AVAIL=$($ADB shell "awk '/MemAvailable/{printf \"%d\", \$2/1024}' /proc/meminfo" | tr -d '\r')
note "可用内存: ${AVAIL}MB"
if [ "${AVAIL:-0}" -lt 250 ]; then
    note "!! 可用内存不足 250MB, 跑语音可能 OOM 黑屏。先停掉重服务或重启板子。"
fi
PORTS=$($ADB shell "netstat -tln 2>/dev/null | grep -cE ':8001|:8004|:8112'" | tr -d '\r')
note "网关端口(8001/8004/8112)在听: $PORTS/3"
if [ "$PORTS" != "3" ]; then
    note "!! 栈不全, 先 /etc/init.d/S98xiaorui start"
fi
WAKE_SRC=$($ADB shell "grep -o '唤醒源: .*' $FELOG 2>/dev/null | tail -1" | tr -d '\r')
note "唤醒源: ${WAKE_SRC:-未知}"

# ---------- 1) 准备素材(加数字增益) ----------
WORK="$OUT/audio"
mkdir -p "$WORK"
AUDIO="$REPO/tests/audio"
# ★ 唤醒词和指令素材都要加同样的增益(2026-11-19 实测踩坑):
#   只放大唤醒词时, 指令是原始音量 -> VAD 能量门把它当噪声丢掉,
#   整句只采到唤醒词, 看起来像"认不到指令"。
#   增益后两者电平一致, VAD 才能把"唤醒词+指令"合成一句。
for f in wake_tts_pure wake_pos004 wake2_time; do
    [ -f "$AUDIO/$f.wav" ] || continue
    ffmpeg -y -loglevel error -i "$AUDIO/$f.wav" -af "volume=${GAIN}dB" "$WORK/$f.wav" 2>/dev/null \
        || cp "$AUDIO/$f.wav" "$WORK/$f.wav"
done
for f in cmd_time cmd_joke cmd_alarm cmd_name cmd_weather cmd_thanks cmd_volume \
         cmd_music cmd_repeat long_story long_sci long_history long_cook long_travel; do
    [ -f "$AUDIO/$f.wav" ] || continue
    ffmpeg -y -loglevel error -i "$AUDIO/$f.wav" -af "volume=${GAIN}dB" "$WORK/$f.wav" 2>/dev/null \
        || cp "$AUDIO/$f.wav" "$WORK/$f.wav"
done
note "素材增益 +${GAIN}dB -> $WORK"

# ---------- 2) 用例表 ----------
# id | 唤醒词素材 | 指令素材 | 期望(人工看)
CASES=(
    "time|wake_tts_pure|cmd_time|报时间"
    "joke|wake_tts_pure|cmd_joke|讲笑话"
    "alarm|wake_tts_pure|cmd_alarm|定闹钟"
    "name|wake_tts_pure|cmd_name|自我介绍"
    "weather|wake_tts_pure|cmd_weather|问天气"
    "thanks|wake_tts_pure|cmd_thanks|道谢"
    "story|wake_tts_pure|long_story|讲长故事"
    "sci|wake_tts_pure|long_sci|讲科幻"
)

play() { # 放一个 wav, 等它播完
    local f="$1"
    [ -f "$f" ] || return 1
    aplay -D "$PLAY_DEV" -q "$f" 2>/dev/null
}

wait_idle() { # 等网关安静下来
    sleep "${IDLE_WAIT:-12}"
}

RESULTS="$OUT/results.md"
{
    echo "# RK3562 板端声学验收报告"
    echo
    echo "- 时间: $(date '+%F %T')"
    echo "- 板子: $SERIAL"
    echo "- 唤醒源: ${WAKE_SRC:-未知}"
    echo "- 可用内存: ${AVAIL}MB"
    echo "- 素材增益: +${GAIN}dB"
    echo
    echo "| 用例 | 唤醒 | ASR 文本 | 回答 | TTS 合成 | 判定 |"
    echo "|---|---|---|---|---|---|"
} > "$RESULTS"

for round in $(seq 1 "$ROUNDS"); do
    note "===== 第 $round/$ROUNDS 轮 ====="
    for entry in "${CASES[@]}"; do
        IFS='|' read -r id wake cmd expect <<< "$entry"
        mark=$($ADB shell cat "$GWLOG" 2>/dev/null | wc -l)
        fmark=$($ADB shell cat "$FELOG" 2>/dev/null | wc -l)
        mark=${mark:-0}; fmark=${fmark:-0}

        note "用例 $id ($expect): 放唤醒词 -> 放指令"
        play "$WORK/$wake.wav" || play "$AUDIO/$wake.wav"
        # 等跟说窗开: 服务端判唤醒要走完 VAD 静音 1.2~1.6s + RockASR 1.4s +
        # 一来一回(约 3s), 指令放早了会被当冷启动轮丢掉(真人说完唤醒词也会
        # 停一下再讲指令, 这里按 5s 间隔走真实节奏)。
        sleep "${GAP:-5}"
        play "$WORK/$cmd.wav" || play "$AUDIO/$cmd.wav"
        wait_idle

        GW_NEW=$($ADB shell cat "$GWLOG" 2>/dev/null | tail -n +$((mark+1)))
        FE_NEW=$($ADB shell cat "$FELOG" 2>/dev/null | tail -n +$((fmark+1)))
        asr=$(echo "$GW_NEW" | grep -oE '识别文本: .*' | tail -1)
        tts=$(echo "$GW_NEW" | grep -oE 'RockTTS 合成\([0-9.]+s\)' | tail -1)
        woke=$(echo "$FE_NEW" | grep -cE 'KWS 命中|WAKEUP\[#|VQE 唤醒')
        ans=$(echo "$GW_NEW" | grep -oE '语音生成成功: .*' | tail -1)
        [ -n "$asr" ] || asr="(无)"
        [ -n "$tts" ] || tts="(无)"
        [ -n "$ans" ] || ans="(无)"
        [ -n "$woke" ] || woke=0
        if [ "$woke" -gt 0 ] 2>/dev/null; then w="是"; else w="否"; fi
        if [ "$asr" != "(无)" ] && [ "$ans" != "(无)" ]; then r="PASS"; else r="FAIL"; fi
        echo "| $id | $w | ${asr#识别文本: } | ${ans#语音生成成功: } | ${tts#RockTTS 合成} | $r |" >> "$RESULTS"
        note "  -> 唤醒=$w ASR=${asr#识别文本: } 回答=${ans#语音生成成功: } $tts [$r]"
    done
done

note "报告: $RESULTS"
cat "$RESULTS"
