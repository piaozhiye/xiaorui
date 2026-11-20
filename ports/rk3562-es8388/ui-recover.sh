#!/bin/sh
# 触摸 UI 一键恢复(手动运行, 板子黑屏/界面不出来时用)。
#
# 两种模式自动切换:
#   - 在板子上直接跑: 本机模式, 不经过 adb
#   - 在 PC 上跑:       adb 模式, sh ui-recover.sh <SERIAL>
#
# 做什么:
#   1. weston 不在 -> 拉起(带超时: S49weston 的 restart 是无限 while 等,
#      直接调会挂死 —— 2026-11-20 实测)
#   2. UI 不在     -> 用 S99 重启(带超时)
#   3. 校验: 都起来了才报 OK, 没起来就打印排查线索
#
# 不碰语音栈(前端/网关/模型): 黑屏只是图形栈的事。
set -u

BOARD_UI=/data/xiaorui-ui/xiaorui_ui
BOARD_INIT=/etc/init.d/S99xiaorui-ui
BOARD_WESTON=/usr/bin/weston

if [ -f "$BOARD_UI" ]; then
    # ---------- 板子本机模式 ----------
    run() { sh -c "$1"; }
    where() {
        if pgrep -x "$1" >/dev/null 2>&1; then echo "在跑"; else echo "没在跑"; fi
    }
else
    # ---------- PC adb 模式 ----------
    SERIAL="${1:-}"
    [ -n "$SERIAL" ] || { echo "用法: $0 SERIAL(在板子上跑则不用参数)" >&2; exit 2; }
    run() { adb -s "$SERIAL" shell "$1"; }
    where() {
        if adb -s "$SERIAL" shell "pgrep -x $1 >/dev/null 2>&1" >/dev/null 2>&1; then
            echo "在跑"
        else
            echo "没在跑"
        fi
    }
fi

echo "== 图形栈状态 =="
echo "  weston: $(where weston)"
echo "  UI    : $(where xiaorui_ui)"

if [ "$(where weston)" = "没在跑" ]; then
    echo "== 拉起 weston =="
    run "kill \$(pgrep -x weston) 2>/dev/null; sleep 1; setsid $BOARD_WESTON >/dev/null 2>&1 </dev/null &"
    sleep 3
fi

if [ "$(where xiaorui_ui)" = "没在跑" ]; then
    echo "== 重启 UI =="
    if run "test -f $BOARD_INIT"; then
        run "sh $BOARD_INIT restart"
    else
        run "setsid sh /data/xiaorui-ui/run.sh >/data/xiaorui-ui/ui.log 2>&1 </dev/null &"
    fi
    sleep 3
fi

echo "== 恢复后 =="
WESTON_STATE=$(where weston)
UI_STATE=$(where xiaorui_ui)
echo "  weston: $WESTON_STATE"
echo "  UI    : $UI_STATE"
if [ "$UI_STATE" = "在跑" ]; then
    echo "OK: 界面已恢复"
    echo "截图: adb shell 'kill -USR1 \$(pgrep -x xiaorui_ui)' && adb pull /tmp/xiaorui-ui.ppm"
else
    echo "!! UI 仍没起来, 看板端日志: /data/xiaorui-ui/ui.log" >&2
    echo "   常见原因: weston 没就绪(wayland socket 未建立) / 缺 libfreetype.so.6" >&2
    exit 1
fi
