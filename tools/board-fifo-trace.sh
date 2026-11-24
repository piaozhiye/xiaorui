#!/bin/bash
# FIFO（Snapclient -> 前端播放环）路径的 ALSA 埋点采集。
#
# 采集内容（全部同一时钟，间隔 ~20ms）：
#   - /proc/asound/card0/pcm0p/sub0/status : state / hw_ptr / appl_ptr / delay / avail
#   - /proc/<前端pid>/io                   : rchar 增量 = FIFO 读取节奏
#   - 前端日志里 "underrun" 行的时间戳
# 另存一次 hw_params（实际的 rate / period / buffer）。
#
# 用法:
#   tools/board-fifo-trace.sh <serial> [秒数] [输出目录]
#
# 前置（脚本不负责起服务）:
#   - 板上前端已带 XR_MUSIC_FIFO 跑起来
#   - snapclient 已在往那个 FIFO 写
set -uo pipefail

SERIAL=${1:?用法: board-fifo-trace.sh <serial> [秒数] [输出目录]}
SECONDS_TRACE=${2:-30}
OUT=${3:-/tmp/opencode/fifo-trace}
STATUS=/proc/asound/card0/pcm0p/sub0/status
HWP=/proc/asound/card0/pcm0p/sub0/hw_params
mkdir -p "$OUT"

PID=$(adb -s "$SERIAL" shell 'pgrep -x xiaorui_live' 2>/dev/null | tr -d '\r' | head -1)
[ -n "$PID" ] || { echo "板上没有 xiaorui_live" >&2; exit 2; }
echo "前端 pid=$PID"

echo "== 实际 hw_params =="
adb -s "$SERIAL" shell "cat $HWP" 2>&1 | tr -d '\r' | tee "$OUT/hw_params.txt"

echo "== 采 ${SECONDS_TRACE}s (20ms 间隔) =="
adb -s "$SERIAL" shell "
CSV=/data/xiaorui/fifo-trace.csv
: > \$CSV
echo 'ts_mono,state,hw_ptr,appl_ptr,delay,avail,rchar' >> \$CSV
END=\$(( \$(date +%s%N)/1000000 + ${SECONDS_TRACE}*1000 ))
while [ \$(( \$(date +%s%N)/1000000 )) -lt \$END ]; do
  TS=\$(date +%s%N)
  ST=\$(grep -E '^(state|hw_ptr|appl_ptr|delay|avail)' $STATUS 2>/dev/null | tr '\n' ' ')
  RC=\$(awk '/^rchar/{print \$2}' /proc/\$PID/io 2>/dev/null)
  echo \"\$TS,\$ST,\$RC\" >> \$CSV
  sleep 0.02
done
" >/dev/null 2>&1
adb -s "$SERIAL" pull /data/xiaorui/fifo-trace.csv "$OUT/" 2>&1 | tail -1
echo "== 前端日志里的 underrun 时刻 =="
adb -s "$SERIAL" shell 'grep -a -n "underrun" /data/xiaorui/frontend.log | tail -20' 2>&1 | tr -d '\r' > "$OUT/underruns.txt"
wc -l < "$OUT/underruns.txt" | xargs -I{} echo "  {} 条"
adb -s "$SERIAL" shell 'rm -f /data/xiaorui/fifo-trace.csv' >/dev/null 2>&1

echo
echo "== 摘要 =="
python3 - "$OUT/fifo-trace.csv" <<'PY'
import csv, re, sys
import numpy as np

rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    p = [x.strip() for x in line.strip().split(",")]
    if len(p) < 7 or not p[0].isdigit():
        continue
    # 列是: ts, "<state/hw_ptr/appl_ptr/delay/avail 拼在一格>", rchar
    try:
        ts_ = int(p[0]); blob = ",".join(p[1:-1])
        try:
            rchar = int(p[-1])
        except ValueError:
            rchar = 0
    except ValueError:
        continue
    def g(name):
        m = re.search(name + r"\s*:?\s*(-?\d+)", blob)
        return int(m.group(1)) if m else 0
    m = re.search(r"state\s*:?\s*(\w+)", blob)
    rows.append((ts_, m.group(1) if m else "?", g("hw_ptr"), g("appl_ptr"),
                 g("delay"), g("avail"), rchar))
if not rows:
    print("  没采到样本"); raise SystemExit(0)

t0 = rows[0][0]
ts = np.array([(r[0] - t0) / 1000.0 for r in rows])
hw = np.array([r[2] for r in rows]); ap = np.array([r[3] for r in rows])
dl = np.array([r[4] for r in rows]); av = np.array([r[5] for r in rows])
io = np.array([r[6] for r in rows])
states = [r[1] for r in rows]

print(f"  样本 {len(rows)} 个, 跨度 {ts[-1]:.1f}s, 平均间隔 {np.mean(np.diff(ts))*1000:.0f} ms")
from collections import Counter
print(f"  state 分布: {dict(Counter(states))}")
print(f"  delay(ms@24k): min {dl.min()/24:.1f} max {dl.max()/24:.1f} 中位 {np.median(dl)/24:.1f}")
print(f"  avail: 中位 {np.median(av):.0f}")
# appl_ptr 增量 = 每次写入量（采样间隔内的聚合）
d_ap = np.diff(ap); d_ap = d_ap[d_ap > 0]
if len(d_ap):
    print(f"  appl_ptr 增量(≈写入量): n={len(d_ap)} 中位 {np.median(d_ap):.0f} "
          f"最小 {d_ap.min()} 最大 {d_ap.max()}   (= {np.median(d_ap)/24:.1f} ms @24k)")
# FIFO 读取节奏
d_io = np.diff(io); d_io = d_io[d_io > 0]
if len(d_io):
    print(f"  FIFO 读增量: n={len(d_io)} 中位 {np.median(d_io):.0f} B "
          f"(= {np.median(d_io)/2/24:.1f} ms @24k/mono) 最大 {d_io.max()}")
# 欠载时刻（state 变 PREPARED/SETUP 或 delay 归零后重新涨）
mask = np.array([s in ("PREPARED", "SETUP") for s in states])
if mask.any():
    print(f"  设备回到非 RUNNING 的次数: {int(mask.sum())}  (首次 t={ts[np.argmax(mask)]:.1f}s)")
print("  提示: 若 appl_ptr 增量(=写入量) 远大于 delay 承载能力, 欠载在消费侧;")
print("        若 FIFO 读增量的间隔 > delay 对应的时长, 欠载在供流侧。")
PY
