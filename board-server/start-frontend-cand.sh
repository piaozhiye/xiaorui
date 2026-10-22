#!/bin/sh
# 板端前端(候选/验收轮)启动器: 杀旧 + 带环境启动 + 就绪判定
# 由验收脚本经 adb 调用(内含 setsid 重定向, adb 调用方能正常返回), 也可板上手动.
# 日志固定 /tmp/xr_live.log; 环境固定 . /data/xiaorui-server/xiaorui.env
LOG=/tmp/xr_live.log
BIN=/data/xiaorui-gateway/xiaorui_live-cand
PROD=/data/xiaorui-server/board-scripts/xiaorui_live
[ -x "$BIN" ] || exit 1

# 列出前端实例 PID. 两处匹配坑都要避开:
#  1) comm 截断到 15 字符(xiaorui_live-cand -> xiaorui_live-ca), 不能用
#     pgrep -x/comm 匹配, 必须读 /proc/*/exe 全路径。
#  2) adb push 是**原地覆盖**: 覆盖后运行中实例的 exe 链接会变成
#     "<path> (deleted)", 精确匹配 "$BIN" 静默失配 -> 旧实例不杀 ->
#     新实例 spk/capture 全是 Device or resource busy, 整轮验收作废。
#     (2026-10-15 实机 A/B 时踩中, 表现为"残留实例数=0"但设备仍被占)
live_pids() {
    for p in /proc/[0-9]*; do
        exe=$(readlink "$p/exe" 2>/dev/null) || continue
        exe=$(echo "$exe" | sed 's/ (deleted)$//')
        case "$exe" in
            "$BIN"|"$PROD") echo "${p#/proc/}" ;;
        esac
    done
}

for pid in $(live_pids); do kill -TERM "$pid" 2>/dev/null; done
# 必须等句柄真正释放, 固定 sleep 不够: 前端收到 TERM 后还会走 restart 分支
# 重新开卡, 抢不到就 sleep(3) 空转, 句柄被它占着。
i=0
while [ "$i" -lt 25 ] && [ -n "$(live_pids)" ]; do sleep 1; i=$((i+1)); done
LEFT=$(live_pids | tr '\n' ' ')
if [ -n "$LEFT" ]; then
    for pid in $LEFT; do kill -9 "$pid" 2>/dev/null; done
    sleep 2
fi


. /data/xiaorui-server/xiaorui.env
cd /data/xiaorui-gateway || exit 1
setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib \
    /data/xiaorui-gateway/xiaorui_live-cand > "$LOG" 2>&1 < /dev/null &
PID=$!

# 就绪判定: 30s 内出现"聆听中"(麦就绪)
i=0
while [ $i -lt 30 ] && ! grep -q '\[xr\] ambient=' "$LOG" 2>/dev/null; do
    kill -0 "$PID" 2>/dev/null || exit 1
    sleep 1; i=$((i+1))
done
if grep -q '\[xr\] ambient=' "$LOG" 2>/dev/null && kill -0 "$PID" 2>/dev/null; then
    echo "frontend: ready"
else
    echo "frontend: 30s 未见聆听中, 查 $LOG"
    exit 1
fi
