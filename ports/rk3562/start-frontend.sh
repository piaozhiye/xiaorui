#!/bin/sh
# RK3562 板端前端启动脚本（放到板上 /data/xiaorui/start-frontend.sh）
#
# 前置（PC 侧一次即可）:
#   adb reverse tcp:8101 tcp:8101     # 板子经 USB 隧道访问 PC 网关
#
# 用法: sh /data/xiaorui/start-frontend.sh [设备名]
set -eu

ENV_FILE=/data/xiaorui/xiaorui.env
[ -f "$ENV_FILE" ] || { echo "缺 $ENV_FILE" >&2; exit 2; }

# shellcheck disable=SC1090
. "$ENV_FILE"

DEV="${1:-}"
if [ -n "$DEV" ]; then
    export XR_MIC="$DEV"
fi

# OOM 优先级: 前端是语音主链路(采集/KWS/播放), 与网关/RKLLM 同档保护(-500),
# 不能被内核当普通进程挑走。从 adb shell 里启动会继承 adbd 的 -950, 那是巧合;
# 开机走 init 时默认是 0 —— 所以在脚本里显式设, 每次启动都生效。
echo -500 > /proc/self/oom_score_adj 2>/dev/null || true

exec /data/xiaorui/bin/xiaorui_live
