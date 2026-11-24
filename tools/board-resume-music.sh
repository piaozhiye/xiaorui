#!/bin/sh
# 板端: 重启 snapclient(恢复音乐) 并量 "客户端启动 -> ALSA RUNNING" 的毫秒数。
# 用法: sh board-resume-music.sh <card> [server]     (A=0, B=1)
CARD=${1:?用法: board-resume-music.sh <card> [server]}
SRV=${2:-127.0.0.1:17104}
STAT="/proc/asound/card$CARD/pcm0p/sub0/status"
cd /data/xiaorui/snapcast || exit 1

pkill -x snapclient 2>/dev/null
sleep 0.3
T0=$(date +%s%N)
setsid ./snapclient "tcp://$SRV" \
    --player="file:filename=/data/xiaorui/snapcast/music.raw" \
    --mixer software --logsink stdout --logfilter '*:warn' > /tmp/sc-pr.log 2>&1 &

j=0
while [ $j -lt 250 ]; do        # 20ms 轮询, 最多 5s
    if grep -q '^state: RUNNING' "$STAT" 2>/dev/null; then
        T1=$(date +%s%N)
        echo "RESUME_MS=$(( (T1 - T0) / 1000000 ))"
        exit 0
    fi
    sleep 0.02
    j=$((j + 1))
done
echo "RESUME_MS=timeout"
exit 1
