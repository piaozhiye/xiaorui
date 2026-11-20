#!/bin/sh
# RK3562 + ES8388 板载麦/喇叭 mixer 设置(开机自启调用)。
#
# 本板与旧 RK809 板、与旧 RK3576 ES8388 板的三点不同:
#   1. amixer 只认 **数字卡号**(amixer -c rockchipes8388 会报 Invalid card number),
#      所以卡号在运行时从 /proc/asound/cards 解析, 不写死。
#   2. 不用 SAI 数字回环(numid 22/18): 那是 VQE 4ch(2mic+2ref) 用的,
#      本板是 2ch 直采, 开了反而会串扰。
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
# DLP(数字回环)模式: 采集变 2ch = [麦克风, 播放数据]。
#   必须在**任何 PCM 打开之前**设置 —— 驱动 dlp_mode_put 在有流运行时会返回
#   -EPERM("Should set this mode before pcm open")。本脚本在开机自启里跑在
#   前端启动之前, 正好满足。
#   依赖设备树给 SAI1 加 rockchip,digital-loopback(见 README 的"设备树/DLP"一节)。
set_control() {  # set_control <名称> <值>  (整数型控件)
    amixer -c "$CARD" cset name="$1" "$2" >/dev/null 2>&1 \
        || { echo "mixer 设置失败: $1 = $2" >&2; exit 1; }
}

set_switch() {   # set_switch <名称> <on|off>  (开关型控件)
    amixer -c "$CARD" sset "$1" "$2" >/dev/null 2>&1 \
        || { echo "mixer 开关设置失败: $1 = $2" >&2; exit 1; }
}

# DLP 模式: "2CH: 1 Mic + 1 Loopback" -> ch0=麦克风, ch1=播放数据(数字参考)。
# 控件名带 DAI 前缀: "SAI1 Software Digital Loopback Mode"。
# 没这个控件(设备树没开 DLP)就跳过, 不影响纯麦克风链路。
set_dlp_mode() {
    if amixer -c "$CARD" cset numid=62 "2CH: 1 Mic + 1 Loopback" >/dev/null 2>&1; then
        echo "DLP: 采集 = 1 Mic(ch0) + 1 Loopback(ch1)"
    else
        echo "DLP: 控件不可用(设备树未开 rockchip,digital-loopback?), 采集按纯麦克风"
    fi
}

# 采集: 板载麦 PGA
set_control "Left Channel Capture Volume" 8
set_control "Right Channel Capture Volume" 8
set_switch "Main Mic" on

# 播放: 板载喇叭
set_switch "Speaker" on
set_switch "spk switch" on
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

# 数字回环参考: 必须在开流之前设置(驱动不允许运行中改)
set_dlp_mode

echo "ES8388 mixer 就绪 (card$CARD): 麦 PGA=8(+24dB), 喇叭开, 输出=15/33(-21dB), DAC=184(解mute)"
