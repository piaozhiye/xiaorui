#!/bin/bash
# PC 侧: 把 PC 的真实时间同步到 RK3562 板子(并写回板上的 RTC)。
#
# 为什么需要: 板子 eth0/wlan0 都是 NO-CARRIER, 完全没有网络, chronyd 校不上;
# RTC 出厂值是 2017-08-04。板子通过 USB 连着 PC, PC 有准的时钟, 所以用
# adb 把纪元传过去是最省事也最可靠的校时手段。
#
# 同步后:
#   - 板子 /etc/localtime 指向 Asia/Shanghai(由 board-time.sh setup 保证),
#     所以 date / datetime.now() 出来的就是本地时间;
#   - 时间写回 rk808-rtc(hwclock -u -w), 这样下次重启内核从 RTC 起步;
#   - last-good-epoch 落盘, 没网重启也能恢复到接近真实的时间。
#
# 用法: ports/rk3562/sync-board-time.sh [serial]
set -euo pipefail

SERIAL="${1:-}"
if [ -z "$SERIAL" ]; then
    SERIAL=$(adb devices | awk '/device$/{print $1}' | head -1)
fi
[ -n "$SERIAL" ] || { echo "没有在线 adb 设备" >&2; exit 2; }

EPOCH=$(date +%s)
echo "== 同步板子时间: $EPOCH ($(date -d "@$EPOCH" '+%F %T %Z')) =="
adb -s "$SERIAL" shell "date -s @$EPOCH"
adb -s "$SERIAL" shell 'hwclock -u -w 2>/dev/null || true'
adb -s "$SERIAL" shell 'sh /data/xiaorui/board-time.sh save 2>/dev/null || true'
echo "== 板端现状 =="
adb -s "$SERIAL" shell 'date; hwclock -u -r'
