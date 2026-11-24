#!/bin/bash
# 板端音乐声学闭环: PC 放音 -> 板子拾音 -> 唤醒 -> 放歌 -> 监测喇叭播放指针
#
# 用法:
#   tools/board-music-acoustic.sh <serial> <wake.wav> <cmd.wav> [gap_s] [gain_db]
#
# 做法(与 tools/run-acoustic-suite.sh 同一套节奏):
#   1) 后台轮询板端 /proc/asound/card0/pcm0p/sub0/status 的 appl_ptr/hw_ptr
#   2) PC 放唤醒词 -> 等 gap -> 放指令
#   3) 指针推进 = 喇叭真的在出声; 网关日志里出现音乐标记 = 走的是音乐链路
set -uo pipefail

SERIAL=${1:?用法: board-music-acoustic.sh <serial> <wake.wav> <cmd.wav> [gap_s] [gain_db]}
WAKE=${2:?用法: board-music-acoustic.sh <serial> <wake.wav> <cmd.wav> [gap_s] [gain_db]}
CMD=${3:?用法: board-music-acoustic.sh <serial> <wake.wav> <cmd.wav> [gap_s] [gain_db]}
GAP=${4:-5}
GAIN=${5:-0}
PLAY_DEV=${PLAY_DEV:-plughw:1,0}
OUT=${OUT:-/tmp/opencode/music-acoustic}
STATUS=/proc/asound/card0/pcm0p/sub0/status

mkdir -p "$OUT"
LOG=/data/xiaorui-gateway/data/gateway.log
PTR="$OUT/pointer.txt"
MARK="$OUT/markers.txt"
FELOG=/data/xiaorui/frontend.log

prepare() { # 需要增益时先生成临时副本
    local src="$1" dst="$2"
    if [ "$GAIN" = "0" ]; then
        echo "$src"
        return
    fi
    ffmpeg -y -loglevel error -i "$src" -af "volume=${GAIN}dB" "$dst" 2>/dev/null || {
        echo "增益失败: $src" >&2
        exit 2
        }
    echo "$dst"
}

WAKE_PLAY=$(prepare "$WAKE" "$OUT/wake_g${GAIN}.wav")
CMD_PLAY=$(prepare "$CMD" "$OUT/cmd_g${GAIN}.wav")
echo "播放素材增益: ${GAIN}dB"

LOGSIZE=$(adb -s "$SERIAL" shell "wc -c < $LOG" | tr -d '\r' | awk '{print $1}')
FESIZE=$(adb -s "$SERIAL" shell "wc -c < $FELOG" | tr -d '\r' | awk '{print $1}')

(
    for _ in $(seq 1 900); do
        adb -s "$SERIAL" shell "cat $STATUS 2>/dev/null | grep -E '^(state|appl_ptr|hw_ptr)'" \
            2>/dev/null | tr -d '\r' | tr '\n' ' ' \
            | sed "s|^|$(date +%s.%N) |" >> "$PTR"
        echo >> "$PTR"
        sleep 0.2
    done
) &
POLLER=$!
sleep 1

echo "== 放唤醒词: $WAKE_PLAY =="
aplay -D "$PLAY_DEV" -q "$WAKE_PLAY" 2>/dev/null || { echo "PC 播放失败" >&2; kill $POLLER; exit 2; }
echo "== 等 ${GAP}s =="
sleep "$GAP"
echo "== 放指令: $CMD_PLAY =="
aplay -D "$PLAY_DEV" -q "$CMD_PLAY" 2>/dev/null || { echo "PC 播放失败" >&2; kill $POLLER; exit 2; }
echo "== 等板子应答 =="
sleep 25

kill $POLLER 2>/dev/null
wait $POLLER 2>/dev/null

adb -s "$SERIAL" shell "tail -c +$((LOGSIZE + 1)) $LOG" 2>/dev/null | tr -d '\r' > "$MARK"
adb -s "$SERIAL" shell "tail -c +$((FESIZE + 1)) $FELOG" 2>/dev/null | tr -d '\r' > "$OUT/frontend.txt"

echo
echo "== 网关: 识别与音乐标记 =="
grep -aoE "识别文本: [^|]*|播放本地音乐: [^|]*|music stream first_audio=[0-9.]+s|电台: [^|]*|music stream finished packets=[0-9]+|音乐流播放失败[^|]*|未找到可播放音乐|唤醒词文本静默忽略[^|]*|云端转写未含唤醒词[^|]*" "$MARK" | head -14
echo
echo "== 前端: KWS 与跟说窗 =="
grep -aoE "KWS 命中: [^ ]*|声学唤醒[^|]*|说完 \([0-9.]+s\)|跟说音频交服务端 ASR|turn=[0-9]+ audio submitted: [0-9]+ samples|空轮[^|]*" "$OUT/frontend.txt" | tail -14
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
runs = []
cur = None
for ts, state, hw, appl in rows:
    if state == "RUNNING":
        if cur is None:
            cur = [ts, ts, hw, appl]
        else:
            cur[1] = ts
            cur[3] = appl
    else:
        if cur is not None:
            runs.append(cur)
            cur = None
if cur is not None:
    runs.append(cur)
print(f"  采样 {len(rows)} 点")
if not runs:
    print("  没有 RUNNING 段 —— 喇叭全程没出声")
for start, end, hw, appl in runs:
    print(f"  RUNNING +{start-base:.2f}s ~ +{end-base:.2f}s  累计 {appl} 帧"
          f"({appl/48000:.2f}s@48k)")
PY
echo
echo "== 原始文件: $OUT =="
