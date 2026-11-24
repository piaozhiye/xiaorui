#!/bin/bash
# 板端音乐喇叭级验证: 用前端的 XR_TEXT_INJECT 钩子绕过麦克风,
# 让前端自己把文本交给网关 -> 网关回音乐 -> 前端在 ES8388 喇叭上播出来。
# 判定: ALSA 播放指针从 0 推进 + 网关日志出现音乐标记。
#
# 用法: tools/board-music-speaker.sh <serial> "<文本>" [等秒数]
#
# 注意: 本脚本会重启板上前端(xiaorui_live), 结束时恢复成不带注入的正常启动。
set -uo pipefail

SERIAL=${1:?用法: board-music-speaker.sh <serial> "<文本>" [秒数]}
TEXT=${2:?用法: board-music-speaker.sh <serial> "<文本>" [秒数]}
WAIT=${3:-45}
STATUS=/proc/asound/card0/pcm0p/sub0/status
OUT=${OUT:-/tmp/opencode/music-speaker}
LOG=/data/xiaorui-gateway/data/gateway.log

mkdir -p "$OUT"
PTR="$OUT/pointer.txt"
# ALSA 的 appl_ptr 是累积量, 上一次跑的样本必须清掉, 否则指针增量会算错
: > "$PTR"

restore_frontend() {
    echo "== 恢复前端(不带注入) =="
    adb -s "$SERIAL" shell 'pkill -x xiaorui_live 2>/dev/null || true; sleep 2' >/dev/null 2>&1
    adb -s "$SERIAL" shell '(setsid sh /data/xiaorui/start-frontend.sh >>/data/xiaorui/frontend.log 2>&1 </dev/null &); true' >/dev/null 2>&1
    for _ in $(seq 1 30); do
        adb -s "$SERIAL" shell 'pgrep -x xiaorui_live' >/dev/null 2>&1 && { echo "  前端已恢复"; return 0; }
        sleep 1
    done
    echo "  !! 前端没起来, 手工查 /data/xiaorui/frontend.log" >&2
    return 1
}
trap restore_frontend EXIT

LOGSIZE=$(adb -s "$SERIAL" shell "wc -c < $LOG" | tr -d '\r' | awk '{print $1}')

echo "== 停前端 =="
adb -s "$SERIAL" shell 'pkill -x xiaorui_live 2>/dev/null || true; sleep 2' >/dev/null 2>&1

echo "== 带注入起前端: $TEXT =="
adb -s "$SERIAL" shell ". /data/xiaorui/xiaorui.env; export XR_TEXT_INJECT='$TEXT'; setsid /data/xiaorui/bin/xiaorui_live >>/data/xiaorui/frontend.log 2>&1 </dev/null &" >/dev/null 2>&1
sleep 3

(
    for _ in $(seq 1 400); do
        adb -s "$SERIAL" shell "cat $STATUS 2>/dev/null | grep -E '^(state|appl_ptr|hw_ptr)'" \
            2>/dev/null | tr -d '\r' | tr '\n' ' ' \
            | sed "s|^|$(date +%s.%N) |" >> "$PTR"
        echo >> "$PTR"
        sleep 0.2
    done
) &
POLLER=$!
echo "== 等 ${WAIT}s =="
sleep "$WAIT"
kill $POLLER 2>/dev/null
wait $POLLER 2>/dev/null

adb -s "$SERIAL" shell "tail -c +$((LOGSIZE + 1)) $LOG" 2>/dev/null | tr -d '\r' > "$OUT/markers.txt"

echo
echo "== 网关音乐标记 =="
grep -aoE "识别文本: [^|]*|播放本地音乐: [^|]*|music stream first_audio=[0-9.]+s|电台: [^|]*|music stream finished packets=[0-9]+|音乐流播放失败[^|]*|未找到可播放音乐" "$OUT/markers.txt" | head -12
echo
echo "== 前端日志 =="
adb -s "$SERIAL" shell 'tail -12 /data/xiaorui/frontend.log' 2>/dev/null | tr -d '\r' | tail -12
echo
echo "== 喇叭播放指针 =="
python3 - "$PTR" <<'PY'
import re, sys
rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    m = re.search(r"^(\d+\.\d+)\s+state:\s*(\S+)\s+hw_ptr\s*:\s*(\d+)\s+appl_ptr\s*:\s*(\d+)", line)
    if m:
        rows.append((float(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))))
if not rows:
    print("  没采到指针样本")
    sys.exit(0)
base = rows[0][0]
runs, cur = [], None
for ts, state, hw, appl in rows:
    if state == "RUNNING":
        cur = [ts, ts, appl] if cur is None else [cur[0], ts, appl]
    else:
        if cur is not None:
            runs.append(cur)
            cur = None
if cur is not None:
    runs.append(cur)
print(f"  采样 {len(rows)} 点")
if not runs:
    print("  没有 RUNNING 段 —— 喇叭全程没出声")
prev_end = 0
for start, end, appl in runs:
    played = max(0, appl - prev_end)
    prev_end = max(prev_end, appl)
    print(f"  RUNNING +{start-base:.2f}s ~ +{end-base:.2f}s  本段播放 {played} 帧"
          f"({played/48000:.2f}s @48kHz)")
PY
echo
echo "== 原始文件: $OUT =="
