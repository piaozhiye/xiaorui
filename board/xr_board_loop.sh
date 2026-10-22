#!/bin/sh
# 常驻语音助手循环: 等说话 -> RKNN Paraformer ASR -> 板端网关 -> USB 口出声.
# 常驻运行: 默认无限轮, 默认唤醒词门控(话里须含 小瑞/小子 才应答).
#
# 用法:
#   sh /data/xiaorui-server/board-scripts/xz_board_loop.sh          # 前台常驻
#   sh /data/xiaorui-server/board-scripts/xz_board_loop.sh 5        # 只跑 5 轮
#   XZ_BOARD_GATE=0 sh .../xz_board_loop.sh                         # 关唤醒门(全响应)
#   sh .../xz_board_loop.sh stop                                    # 停掉后台循环
# 后台常驻示例:
#   setsid sh /data/xiaorui-server/board-scripts/xz_board_loop.sh > /tmp/xz_loop.log 2>&1 &
DIR=/data/xiaorui-server/board-scripts
PIDFILE=/tmp/xz_loop.pid

if [ "${1:-}" = stop ]; then
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null && rm -f "$PIDFILE" && echo "[loop] stopped"
    else
        echo "[loop] no pidfile, not running?"
    fi
    exit 0
fi

ROUNDS="${1:-0}"    # 0 = 无限
# 防双开: 已有活循环就不再起第二个
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[loop] already running (pid $(cat "$PIDFILE")), nothing to do"
    exit 0
fi
i=0
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; echo "[loop] bye"; exit 0' INT TERM

# 循环模式默认开唤醒门, 避免常驻时对每句话都应答
GATE="${XZ_BOARD_GATE:-1}"
echo "[loop] 常驻模式, 唤醒门=${GATE}(1=须含 小x 字样), 喊 '小瑞小瑞, xxx' 即应答; 应答后下一轮免唤醒可直接说"

while :; do
    i=$((i + 1))
    [ "$ROUNDS" -gt 0 ] && echo "[loop] ===== 第 $i/$ROUNDS 轮 =====" \
                        || echo "[loop] ===== 第 $i 轮$( [ "$GATE" = 0 ] && echo ' (跟说, 免唤醒)' ) ====="
    XZ_BOARD_GATE="$GATE" sh "$DIR/xz_board_turn.sh" 10
    code=$?
    case "$code" in
        0) GATE=0 ;;     # 刚应答过: 下一轮跟说免唤醒
        42) GATE=1 ;;    # 唤醒门拦下: 恢复要唤醒词
        3) echo "[loop] 本轮没听清(无文本), 继续" ;;
        4) echo "[loop] 本轮电平过低, 继续" ;;
        *) echo "[loop] 轮次异常 exit=$code, 继续" ;;
    esac
    [ "$ROUNDS" -gt 0 ] && [ "$i" -ge "$ROUNDS" ] && break
    sleep 0.3
done
rm -f "$PIDFILE"
echo "[loop] 完成 $ROUNDS 轮"
