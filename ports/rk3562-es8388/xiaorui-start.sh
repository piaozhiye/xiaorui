#!/bin/sh
# 桌面图标「小瑞」: 启动整套小瑞(触摸 UI + 语音栈 + HA)。
#
# 桌面图标通过 weston-desktop-shell 的 [desktop-launcher] path= 调本脚本。
# 幂等: 已经在跑的部件不重复起(S98/S99 的 start 自身幂等)。
#
# 各部分启动都不快, 本脚本不等它们就绪就返回:
#   - 前端/网关: 秒级起来, 但模型首轮预热要 2~3 分钟
#   - HA: ~40s(start-ha.sh 自己后台化)
#   - UI: 依赖 weston 已就绪
set -u

echo "[xiaorui-start] 启动小瑞整套..."
if [ -f /etc/init.d/S98xiaorui ]; then
    # 板内栈 + 前端 + HA(有 .ha_enabled 时)+ crond 看护, 一次到位
    sh /etc/init.d/S98xiaorui start
else
    echo "!! 缺 /etc/init.d/S98xiaorui, 语音栈未启动" >&2
fi
if [ -f /etc/init.d/S99xiaorui-ui ]; then
    sh /etc/init.d/S99xiaorui-ui start
else
    echo "!! 缺 /etc/init.d/S99xiaorui-ui, 触摸界面未启动" >&2
fi
echo "[xiaorui-start] 已下发, 前端首轮预热约需 2~3 分钟"
