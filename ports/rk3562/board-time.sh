#!/bin/sh
# RK3562 板端时间: 时区固定 Asia/Shanghai, 时间源三级回退。
#
# 为什么需要这个脚本(2026-11-11 实测):
#   1) 板子 eth0/wlan0 都是 NO-CARRIER, 完全没有网络 -> chronyd 校不上;
#   2) RTC 是 rk808-rtc, 出厂值 2017-08-04 -> 板上日志时间戳全是错的;
#   3) 网关报时走 datetime.now()(本地时区), 之前只能人工把系统钟拨成
#      "UTC+8 的墙上时间"来凑对。现在改成: 真 UTC 纪元 + Asia/Shanghai 时区。
#
# 开机时按序取第一个"看起来合理"的时间源:
#   1) RTC          hwclock -s。前提: RTC 里存的是真时间(年份 >= 2020)。
#   2) last-good    /data/xiaorui/last-good-epoch —— 上次拿到真时间时留存的。
#   3) 兜底          保持内核默认值, 只把时区设对。
#
# 运行期:
#   - chronyd(/etc/init.d/S49chronyd, 已带 pool.ntp.org + makestep 1.0 3 +
#     rtcsync)有网时自动校时, 校上后由 rtcsync 把真时间写回 RTC;
#   - 本脚本另起一个后台循环, 每分钟把"当前时间看起来合理"时的 epoch 存进
#     last-good-epoch, 这样下次没网重启也能恢复到接近真实的时间。
#
# 用法: sh /data/xiaorui/board-time.sh [setup|save]
#   setup  开机时调一次: 设时区 + 恢复时间 + 拉起守护循环
#   save   只把当前时间存进 last-good-epoch(守护循环和 PC 侧同步脚本用)
set -u

TZ_NAME=Asia/Shanghai
STATE_DIR=/data/xiaorui
EPOCH_FILE="$STATE_DIR/last-good-epoch"
PID_FILE="$STATE_DIR/board-time-watch.pid"
# 2020-01-01T00:00:00Z。低于这个值的时间一律当垃圾(出厂 RTC 是 2017)。
MIN_VALID=1577836800

log() { echo "[board-time] $*"; }

# 时区: /etc/localtime 软链 + /etc/timezone, 两者都写(C/Python 读前者,
# 少数 busybox 工具读后者)。
setup_tz() {
    if [ -e /usr/share/zoneinfo/"$TZ_NAME" ]; then
        ln -sfn ../usr/share/zoneinfo/"$TZ_NAME" /etc/localtime 2>/dev/null
        echo "$TZ_NAME" > /etc/timezone 2>/dev/null
        log "时区 = $TZ_NAME"
    else
        log "警告: 板上没有 zoneinfo/$TZ_NAME, 时区保持原样"
    fi
    TZ="$TZ_NAME"
    export TZ
}

now_epoch() { date +%s 2>/dev/null || echo 0; }

# $1 = epoch, 0 = 不可信
is_valid() {
    case "${1:-0}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge "$MIN_VALID" ]
}

# RTC 里存的是 UTC 纪元(内核 sync_hw_clock 与 chrony rtcsync 都是写 UTC),
# 所以读/写都要带 -u。
read_rtc_epoch() {
    _s=$(hwclock -u -r 2>/dev/null) || return 1
    [ -n "$_s" ] || return 1
    # hwclock -u -r 输出的是**本地时区钟面**(RTC 的 UTC 值 + TZ 换算后的显示),
    # 必须用本地时区的 date 解析才能还原出正确的 UTC 纪元。
    # 2026-11-11 实测 bug: 原来写 date -u -d —— 把 CST 钟面当 UTC 解析,
    # epoch 凭空 +8h -> setup 把钟拨到"明天 06:00" -> write_rtc 又把 +8h
    # 写回 RTC -> chronyd 拉回但 last-good 已被污染, 每次开机复现
    # (stack.log 时间戳 06:0x = 真实 22:0x + 8h)。
    _e=$(date -d "$_s" +%s 2>/dev/null) || return 1
    [ -n "$_e" ] || return 1
    echo "$_e"
}

write_rtc() {
    hwclock -u -w 2>/dev/null && log "已把当前时间写回 RTC"
}

save_good() {
    _e=$(now_epoch)
    if is_valid "$_e"; then
        mkdir -p "$STATE_DIR" 2>/dev/null
        echo "$_e" > "$EPOCH_FILE" 2>/dev/null
    fi
}

setup() {
    setup_tz

    # 1) RTC
    _rtc=$(read_rtc_epoch || echo "")
    if [ -n "$_rtc" ] && is_valid "$_rtc"; then
        if date -s "@$_rtc" 2>/dev/null; then
            log "时间源 = RTC ($_rtc)"
            write_rtc
            save_good
            return 0
        fi
    elif [ -n "$_rtc" ]; then
        log "RTC 时间无效(epoch=$_rtc, 早于 2020), 不用它"
    fi

    # 2) last-good-epoch
    if [ -f "$EPOCH_FILE" ]; then
        _good=$(cat "$EPOCH_FILE" 2>/dev/null)
        if is_valid "$_good"; then
            if date -s "@$_good" 2>/dev/null; then
                log "时间源 = last-good-epoch ($_good)"
                return 0
            fi
        fi
    fi

    # 3) 兜底
    log "没有可用时间源, 保持系统默认时间(时区已设对)"

    # chronyd 有网时会把系统钟和 RTC 一起校对; 这里主动催一次 makestep,
    # 让"开机钟很离谱但网络已通"的情况立刻纠正。
    if command -v chronyc >/dev/null 2>&1; then
        chronyc makestep >/dev/null 2>&1 || true
    fi
}

# 守护: 每分钟把"看起来合理"的时间存进 last-good-epoch。
# 判据除了年份, 还要求"不比上次存的旧" —— 防止某个错误来源把时间拨回去。
watch_loop() {
    while :; do
        _e=$(now_epoch)
        if is_valid "$_e"; then
            _old=$(cat "$EPOCH_FILE" 2>/dev/null || echo 0)
            case "${_old:-0}" in ''|*[!0-9]*) _old=0 ;; esac
            if [ "$_e" -ge "$_old" ]; then
                mkdir -p "$STATE_DIR" 2>/dev/null
                echo "$_e" > "$EPOCH_FILE" 2>/dev/null
            fi
        fi
        sleep 60
    done
}

case "${1:-setup}" in
    setup)
        setup
        # 只拉起一个守护循环。进程名是 "sh", 不能用 pidof 判, 用 pidfile:
        # 先杀掉旧的, 再起新的, 等它把 pidfile 写出来。
        if [ -f "$PID_FILE" ]; then
            kill "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || true
            rm -f "$PID_FILE"
        fi
        setsid sh "$0" watch >/dev/null 2>&1 </dev/null &
        i=0
        while [ $i -lt 20 ]; do
            [ -f "$PID_FILE" ] && break
            sleep 1
            i=$((i + 1))
        done
        ;;
    save)
        save_good
        ;;
    watch)
        echo $$ > "$PID_FILE"
        watch_loop
        ;;
    *)
        echo "用法: $0 {setup|save|watch}" >&2
        exit 1
        ;;
esac
exit 0
