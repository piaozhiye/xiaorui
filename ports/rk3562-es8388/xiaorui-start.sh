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
# 顺序很重要(2026-11-20 用户实测): 先起触摸界面(秒级, 点了立刻有反应),
# 语音栈(网关/模型/HA, 慢)放后台 —— 反过来的话界面要等几十秒才出现,
# 用户看着像"点了没反应"。
if [ -f /etc/init.d/S99xiaorui-ui ]; then
    sh /etc/init.d/S99xiaorui-ui start
else
    echo "!! 缺 /etc/init.d/S99xiaorui-ui, 触摸界面未启动" >&2
fi
if [ -f /etc/init.d/S98xiaorui ]; then
    # ★ 用 restart 而不是 start(2026-11-20 真板反馈: 点图标要听到"下午好,
    #   现在是…, 小瑞已经准备好了")。问候语由网关在**前端连上时**(hello)
    #   触发, 而网关有 10 分钟去重 —— 只 start 的话网关还活着、不会重发,
    #   前端也没重连, 于是点了图标什么声音都没有。restart 会让网关进程
    #   重来(去重标记归零)+ 前端重连 -> 问候语自然播出。
    #   前端启动后的预热期界面会显示"小瑞正在准备语音…"(见 PREPARING 状态)。
    (setsid sh /etc/init.d/S98xiaorui restart >/dev/null 2>&1 </dev/null &)
else
    echo "!! 缺 /etc/init.d/S98xiaorui, 语音栈未启动" >&2
fi
echo "[xiaorui-start] 界面已起, 语音栈后台启动中(前端首轮预热约 2~3 分钟)"
