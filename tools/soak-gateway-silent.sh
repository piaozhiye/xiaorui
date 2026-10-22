#!/bin/bash
# 网关无声浸泡: 板内客户端直连 8001 真云轮次, 板/PC 扬声器全程不响.
# 用途: 声学验收(要放音)之前的网关层稳定性证据 -- 云 ASR/LLM/板 Matcha 合成
# 全链真实调用, 网关进程资源采样. 不替代规范要求的声学 30min soak(那条测
# 前端全链), 但覆盖网关进程本身的稳定性与资源趋势.
# 用法: tools/soak-gateway-silent.sh [轮数] [输出目录]
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
ADB="adb ${SERIAL:+-s $SERIAL}"
N="${1:-30}"
OUT="${2:-$REPO/test-results/gwsoak-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
echo "[gwsoak] n=$N out=$OUT"

# 准备音频(真录音 16k 重采样, 板 /tmp 重启即失)
$ADB shell "/data/miniforge/bin/python -c \"
import wave, numpy as np
w = wave.open('/data/kws-corp/pos004.wav'); d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
r = 16000; x = np.interp(np.arange(0, len(d)*r/22050, r/22050), np.arange(len(d)), d.astype(np.float64)).astype(np.int16)
o = wave.open('/tmp/pos004_16k.wav','wb'); o.setnchannels(1); o.setsampwidth(2); o.setframerate(r); o.writeframes(x.tobytes()); o.close()
\""

# 资源采样(每分钟): 网关/前端/8000/rkllm
( START=$(date +%s)
  while [ $(( $(date +%s) - START )) -lt $(( N * 45 + 300 )) ]; do
    $ADB shell "date -u +%FT%TZ; for p in \$(pgrep -f 'xiaorui_live|xiaozhi-gateway/server/app.py|xiaozhi-server/server/app.py|llm_rkllm.py'); do echo \"pid=\$p rss=\$(awk '/VmRSS/{print \$2}' /proc/\$p/status 2>/dev/null) fd=\$(ls /proc/\$p/fd 2>/dev/null | wc -l) th=\$(awk '/Threads/{print \$2}' /proc/\$p/status 2>/dev/null)\"; done" >> "$OUT/monitor.csv" 2>&1
    sleep 60
  done ) &
MON=$!

ok=0; fail=0
for i in $(seq 1 "$N"); do
    TS=$(date -u +%FT%TZ)
    if $ADB shell "/data/miniforge/bin/python /data/xiaorui-gateway/gateway_client.py --url ws://127.0.0.1:8001/xiaorui/v1/ --real-api --scenario both --turns 2 --audio /tmp/pos004_16k.wav 2>/dev/null" | grep -q '"verdict": "PASS"'; then
        ok=$((ok+1)); echo "$TS ok $i" >> "$OUT/turns.txt"
    else
        fail=$((fail+1)); echo "$TS FAIL $i" >> "$OUT/turns.txt"
    fi
    sleep 8
done
kill $MON 2>/dev/null
echo "RESULT ok=$ok fail=$fail n=$N" | tee "$OUT/summary.txt"
python3 "$REPO/tools/analyze-monitor.py" "$OUT/monitor.csv" | tee -a "$OUT/summary.txt"
[ "$fail" -eq 0 ]
