#!/bin/sh
# RK3562 + ES8388 板载麦/喇叭 mixer 设置(开机自启调用)。
#
# 本板与旧 RK809 板、与旧 RK3576 ES8388 板的三点不同:
#   1. amixer 只认 **数字卡号**(amixer -c rockchipes8388 会报 Invalid card number),
#      所以卡号在运行时从 /proc/asound/cards 解析, 不写死。
#   2. VQE 使用 SAI1 硬件回采: SDI0=两路麦克风, SDI1=SDO0 两路参考。
#      不设置 Software Digital Loopback Mode(numid=62), 也不启用 SDI0 loopback。
#   3. 没有 RK809 的 "ADC Capture Volume"/"Playback Path" 之类控件。
#
# 采集增益: Left/Right Channel Capture Volume = 8 (0..8 档, 每档 3dB -> +24dB)。
#   实测(2026-11-16): PC 放真人录音, 板载麦
#     档位 3 (+9dB) -> 说话段 rms -50.2 dBFS / peak -38.6 dBFS
#     档位 8 (+24dB)-> 说话段 rms -42.6 dBFS / peak -18.3 dBFS, 削波 0%
#   底噪两档都是 -58 dBFS(噪声不随 PGA 放大, 信噪比反而变好)。
# 播放音量: Output 1/2 Playback Volume = 15/33 (-21dB)。
#   用户反馈原档 27/33 (-4.5dB) 太大: 21(-13.5dB) 仍偏大, 19(-16.5dB) 仍偏大,
#   再降 4 档 -> 15/33, 相对原档共 -16.5dB。每档 1.5dB。
set -eu

CARD="${XR_AUDIO_CARD:-}"
if [ -z "$CARD" ]; then
    CARD=$(awk '/^[[:space:]]*[0-9]+ \[/ {print $1; exit}' /proc/asound/cards)
fi
[ -n "$CARD" ] || { echo "找不到声卡" >&2; exit 1; }

# amixer 在这块板上的两种写法(实测):
#   整数型控件: cset name="Left Channel Capture Volume" 8   (name= 必须带)
#   开关型控件: sset "Speaker" on                              (用 sset, 不能用 name=)
#
set_control() {  # set_control <名称> <值>  (整数型控件)
    amixer -c "$CARD" cset name="$1" "$2" >/dev/null 2>&1 \
        || { echo "mixer 设置失败: $1 = $2" >&2; exit 1; }
}

set_enum() {      # set_enum <名称> <选项标签>
    amixer -c "$CARD" cset name="$1" "$2" >/dev/null 2>&1 \
        || { echo "mixer 设置失败: $1 = $2" >&2; exit 1; }
}

set_switch() {   # set_switch <名称> <on|off>  (开关型控件)
    amixer -c "$CARD" sset "$1" "$2" >/dev/null 2>&1 \
        || { echo "mixer 开关设置失败: $1 = $2" >&2; exit 1; }
}

# 采集: 板载麦 PGA
set_control "Left Channel Capture Volume" 8
set_control "Right Channel Capture Volume" 8
set_switch "Main Mic" on

# 播放: 板载喇叭
set_switch "Speaker" on
set_switch "spk switch" on
# AIOT DEMO LP4X V10 的外置功放由 GPIO1_C1/GPIO49 高电平使能。当前 6.1
# DTB 若漏了 spk-con-gpio，内核控件写入会成功但读回仍为 off，GPIO 也不会被驱动。
# 优先使用内核控件；读回未开时用板级 GPIO 兜底，避免“脚本成功、功放仍关”的假象。
if ! amixer -c "$CARD" cget name="spk switch" 2>/dev/null | grep -q ': values=on'; then
    GPIO_ROOT=${XR_GPIO_SYSFS_ROOT:-/sys/class/gpio}
    if [ -d "$GPIO_ROOT" ] && [ -w "$GPIO_ROOT/export" ]; then
        if [ ! -d "$GPIO_ROOT/gpio49" ]; then
            echo 49 > "$GPIO_ROOT/export" 2>/dev/null || true
        fi
        if [ -d "$GPIO_ROOT/gpio49" ] && [ -w "$GPIO_ROOT/gpio49/direction" ] \
            && [ -w "$GPIO_ROOT/gpio49/value" ]; then
            echo out > "$GPIO_ROOT/gpio49/direction"
            echo 1 > "$GPIO_ROOT/gpio49/value"
            echo "功放备用使能: GPIO1_C1/GPIO49=high (DTB 未提供有效 spk-con 控件)"
        else
            echo "警告: spk switch 未读回 on，且 GPIO49 不能导出；请在 DTS 配置 spk-con-gpio" >&2
        fi
    else
        echo "警告: spk switch 未读回 on，GPIO sysfs 不可用；请在 DTS 配置 spk-con-gpio" >&2
    fi
fi
set_control "Output 1 Playback Volume" 15
set_control "Output 2 Playback Volume" 15
# ★ DAC 数字音量 + bit7 解 mute(2026-11-20 实机事故):
#   这个控件是**出声闸门** —— 值为 0 时整路 DAC 静音, 声卡流会卡在
#   SETUP 状态不推进, 表现是"语音链路全对(ASR/LLM/TTS/字幕都对), 但喇叭
#   一声不吭"。
#   板上的 S42audio-es8388 也会设它(=184,184, 安卓对照板实测值), 但那是
#   开机极早期跑的, 早于 codec 就绪, cset 失败被 >/dev/null 2>&1 吞掉,
#   值就留在默认 0 —— **每次重启都会复发**, 而 S42 不在本仓管理范围内。
#   所以这里(S98 跑, 晚于 S42 且前端启动之前)再设一遍, 幂等且失败即报错。
set_control "PCM Volume" 184,184

# 4-channel capture routing: SDI0 carries the two mic lanes; SDI1 carries
# the two SAI loopback reference lanes sourced from SDO0.
set_enum "SAI1 SDI0 Loopback Src Select" "From SDO0"
set_enum "SAI1 SDI0 Loopback Switch" "Disable"
set_enum "SAI1 SDI1 Loopback Src Select" "From SDO0"
set_enum "SAI1 SDI1 Loopback Switch" "Enable"

echo "ES8388 mixer 就绪 (card$CARD): SDI0=2 MIC, SDI1=2 SDO0 ref; 不使用软件 DLP"
echo "麦 PGA=8(+24dB), 喇叭开, 输出=15/33(-21dB), DAC=184(解mute)"
