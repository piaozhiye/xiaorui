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

exec /data/xiaorui/bin/xiaorui_live
