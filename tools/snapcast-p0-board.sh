#!/bin/bash
# 板端 Snapcast 冒烟(等价于计划 P0 的板端验证步骤)。
#
# 用法:
#   tools/snapcast-p0-board.sh <serial> [选项]
#
# 选项:
#   --server <host:port>   板端要连的 snapserver(默认 127.0.0.1:1704, 走 adb reverse 隧道)
#   --soundcard <name|idx> ALSA 播放设备(默认 8 = sysdefault:CARD=rockchipes8388)
#   --fifo <path>          改用 file/FIFO 出口(不碰声卡, 可与前端同时跑)
#   --keep-frontend        不停前端(默认会停前端, 因为 hw 播放设备独占)
#   --seconds <n>          采样时长秒数(默认 8)
#
# 行为:
#   1) 记录前端/端口/设备占用状态
#   2) (默认)停前端腾出 hw 播放设备
#   3) 起 snapclient, 采 ALSA 指针(ALSA 模式)或 FIFO 落盘速率(FIFO 模式)
#      + RSS/线程 + CPU
#   4) 结束客户端, 查设备是否释放
#   5) 恢复前端(不带注入), 确认端口与进程回来了
#   6) 有任何一步不达标就以非零码退出(方便脚本化断言)
#
# 注意: 本脚本不改 ES8388 mixer(客户端用 --mixer software, 该模式下
# snapclient 根本不会打开 mixer 控制接口, 只做只读音量上报)。
set -uo pipefail

SERIAL=${1:?用法: snapcast-p0-board.sh <serial> [选项]}
shift || true

SERVER="127.0.0.1:1704"
SOUNDCARD="8"
FIFO=""
KEEP_FRONTEND=0
SECONDS_SAMP=8
RC=0

while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="$2"; shift 2 ;;
        --soundcard) SOUNDCARD="$2"; shift 2 ;;
        --fifo) FIFO="$2"; shift 2 ;;
        --keep-frontend) KEEP_FRONTEND=1; shift ;;
        --seconds) SECONDS_SAMP="$2"; shift 2 ;;
        *) echo "未知选项: $1" >&2; exit 2 ;;
    esac
done

CLIENT=/data/xiaorui/snapcast/snapclient
STATUS=/proc/asound/card0/pcm0p/sub0/status
OUT=${OUT:-/tmp/opencode/snapcast-p0}
mkdir -p "$OUT"

if ! adb -s "$SERIAL" shell "test -x $CLIENT" >/dev/null 2>&1; then
    echo "板上没有 $CLIENT, 先用 tools/snapcast-build-client.sh 交叉编译再 adb push" >&2
    exit 2
fi

restore_frontend() {
    if [ "$KEEP_FRONTEND" = "1" ]; then return 0; fi
    echo
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

echo "== 记录现场 =="
adb -s "$SERIAL" shell 'echo "  前端 pid: $(pgrep -x xiaorui_live)"; for p in 8112 8001 8004; do /data/gwvenv/bin/python /data/xiaorui-gateway/probe-port.py $p >/dev/null 2>&1 && echo "  $p OK" || echo "  $p NO"; done; if fuser -s /dev/snd/pcmC0D0p 2>/dev/null; then echo "  播放设备: 被占用"; else echo "  播放设备: 空闲"; fi' 2>&1 | tr -d '\r'

if [ "$KEEP_FRONTEND" != "1" ]; then
    echo "== 停前端(腾出 hw 播放设备) =="
    adb -s "$SERIAL" shell 'pkill -x xiaorui_live 2>/dev/null || true; sleep 2' >/dev/null 2>&1
fi

if [ -n "$FIFO" ]; then
    # FIFO 出口: 客户端不碰声卡, 前端可以继续跑
    echo "== 准备 FIFO: $FIFO =="
    # 注意: snapclient 的 file 后端用 fopen(path,"wb") 打开出口, **FIFO 没有读者时
    # 它会卡在 open 上**(实测)。所以必须先起一个读者, 生产上前端就是那个读者。
    adb -s "$SERIAL" shell "rm -f '$FIFO' /tmp/p0fifo.out; mkfifo '$FIFO'" >/dev/null 2>&1
    adb -s "$SERIAL" shell "(setsid sh -c 'cat $FIFO > /tmp/p0fifo.out' >/dev/null 2>&1 &); true" >/dev/null 2>&1
    sleep 1
    echo "== 启动 snapclient (file/FIFO 出口) =="
    # setsid: 让客户端在 adb shell 退出后继续活着
    adb -s "$SERIAL" shell "(setsid '$CLIENT' tcp://$SERVER --player='file:filename=$FIFO' --mixer software --logsink stdout --logfilter '*:info' > /tmp/snapclient.log 2>&1 &); true" >/dev/null 2>&1
else
    echo "== 启动 snapclient (ALSA 出口: $SOUNDCARD) =="
    adb -s "$SERIAL" shell "(setsid '$CLIENT' tcp://$SERVER --soundcard '$SOUNDCARD' --player alsa --mixer software --logsink stdout --logfilter '*:info' > /tmp/snapclient.log 2>&1 &); true" >/dev/null 2>&1
fi
sleep 4

CPID=$(adb -s "$SERIAL" shell 'pgrep -x snapclient | head -1' 2>/dev/null | tr -d '\r')
echo "== snapclient pid: ${CPID:-没起来} =="
if [ -z "$CPID" ]; then
    echo "  !! 客户端没起来" >&2
    adb -s "$SERIAL" shell 'head -20 /tmp/snapclient.log' 2>&1 | tr -d '\r'
    RC=1
fi
echo "== 客户端日志(前 20 行) =="
adb -s "$SERIAL" shell 'head -20 /tmp/snapclient.log' 2>&1 | tr -d '\r'

if [ -n "$FIFO" ]; then
    # FIFO 模式不碰声卡 -> ALSA 指针是**前端**的, 不能拿来当 snapclient 的证据
    echo
    echo "== FIFO 落盘速率(FIFO 模式不碰声卡, 所以不采 ALSA 指针) =="
    S0=$(adb -s "$SERIAL" shell "stat -c %s /tmp/p0fifo.out 2>/dev/null || echo 0" 2>/dev/null | tr -d '\r')
    T0=$(date +%s.%N)
    sleep "$SECONDS_SAMP"
    S1=$(adb -s "$SERIAL" shell "stat -c %s /tmp/p0fifo.out 2>/dev/null || echo 0" 2>/dev/null | tr -d '\r')
    T1=$(date +%s.%N)
    awk -v a="$S0" -v b="$S1" -v t0="$T0" -v t1="$T1" 'BEGIN{
        dt = t1 - t0; d = b - a;
        if (dt <= 0 || d <= 0) { print "  没采到数据(FIFO 没增长)"; exit 1 }
        printf "  %d -> %d bytes / %.2fs = %.0f B/s\n", a, b, dt, d/dt;
        printf "  48k/2ch/s16le 的实时速率应为 192000 B/s, 实测比值 %.2f\n", (d/dt)/192000;
    }' || RC=1
else
    echo
    echo "== ALSA 播放指针(证明在实时写声卡) =="
    PTR="$OUT/pointer.txt"
    : > "$PTR"
    for _ in $(seq 1 "$SECONDS_SAMP"); do
        adb -s "$SERIAL" shell "cat $STATUS 2>/dev/null | grep -E '^(state|hw_ptr|appl_ptr)'" 2>/dev/null \
            | tr -d '\r' | tr '\n' ' ' | sed "s|^|$(date +%s.%N) |" >> "$PTR"
        echo >> "$PTR"
        sleep 1
    done
    cat "$PTR"
    python3 - "$PTR" <<'PY' || RC=1
import re, sys
rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    m = re.search(r"^(\d+\.\d+).*?hw_ptr\s*:\s*(\d+).*?appl_ptr\s*:\s*(\d+)", line)
    if m:
        rows.append((float(m.group(1)), int(m.group(2)), int(m.group(3))))
if len(rows) < 2:
    print("  样本不足, 算不出速率")
    sys.exit(1)
t0, h0, a0 = rows[0]
t1, h1, a1 = rows[-1]
dt = t1 - t0
print(f"  时间跨度 {dt:.2f}s  hw_ptr {h0}->{h1}  appl_ptr {a0}->{a1}")
print(f"  硬件消耗速率 {((h1-h0)/dt):.0f} 样本/s   应用写入速率 {((a1-a0)/dt):.0f} 样本/s")
print(f"  缓冲差(appl-hw) {a1-h1} 样本 = {(a1-h1)/((h1-h0)/dt)*1000:.1f} ms")
PY
fi

if [ -n "$CPID" ]; then
    echo
    echo "== 内存 / 线程 =="
    adb -s "$SERIAL" shell "grep -E '^(VmHWM|VmRSS|RssAnon|Threads)' /proc/$CPID/status" 2>&1 | tr -d '\r'
    echo "== CPU(3s 窗口, 单核百分比) =="
    T1=$(adb -s "$SERIAL" shell "awk '{print \$14+\$15}' /proc/$CPID/stat" 2>/dev/null | tr -d '\r' | awk '{print $1}')
    sleep 3
    T2=$(adb -s "$SERIAL" shell "awk '{print \$14+\$15}' /proc/$CPID/stat" 2>/dev/null | tr -d '\r' | awk '{print $1}')
    if [ -n "${T1:-}" ] && [ -n "${T2:-}" ]; then
        awk -v a="$T1" -v b="$T2" 'BEGIN{printf "  ticks %.0f -> %.0f = %.1f%% 单核\n", a, b, (b-a)*100/3/100}'
    else
        echo "  采样失败"
    fi
fi

echo
echo
echo "== 客户端日志(尾部) =="
adb -s "$SERIAL" shell 'tail -8 /tmp/snapclient.log' 2>&1 | tr -d '\r'

echo
echo "== 结束客户端, 查设备释放 =="
adb -s "$SERIAL" shell 'pkill -x snapclient 2>/dev/null; sleep 1' >/dev/null 2>&1
if [ "$KEEP_FRONTEND" = "1" ]; then
    # 前端还在跑, 设备本来就该被前端占着
    adb -s "$SERIAL" shell 'if fuser -s /dev/snd/pcmC0D0p 2>/dev/null; then echo "  设备被前端占着(预期)"; else echo "  !! 设备没人占, 前端可能没拿到声卡"; RC=1; fi' 2>&1 | tr -d '\r'
else
    adb -s "$SERIAL" shell 'if fuser -s /dev/snd/pcmC0D0p 2>/dev/null; then echo "  !! 仍占用"; else echo "  已释放"; fi' 2>&1 | tr -d '\r'
fi

if [ -n "$FIFO" ]; then
    echo "== 清理 FIFO 与落盘(别把 PCM 堆在板子上) =="
    adb -s "$SERIAL" shell "pkill -f 'cat $FIFO' 2>/dev/null; rm -f '$FIFO' /tmp/p0fifo.out /tmp/snapclient.log" >/dev/null 2>&1
fi

exit "$RC"
