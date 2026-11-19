#!/bin/bash
# RK3562: 安装 rkaudio_vqe ALSA 插件(6mic_2ref), 生成合并后的 /etc/asound.conf
#
# 板端落点:
#   /usr/lib/alsa-lib/libasound_module_pcm_rkaudio_vqe.so   ALSA 插件
#   /usr/lib/librkaudio_{vqe,common,nn}.so                 插件依赖
#   /usr/share/rkaudio/config_aivqe_new.json              算法配置(开 AEC/AGC)
#   /usr/share/rkaudio/wopt_6mic_ring_35mm_8input_8output.bin  波束权重
#   /etc/asound.conf                                       VQE 采集 + RK809 播放
#
# 生成虚拟设备: rkaudio_vqe_16k (1ch/16k; 输入 8ch = 阵列 6 麦 + RK809 回采 2ch)
#
# 授权: 多麦(>2mic) 的 AI/波束能力需要 Rockchip license。
#      XR_VQE_LIB_DIR 可指定配套库目录，默认使用插件包提供的库。
#      使用前确认 SDK 授权；所需 license.lic 放 /usr/share/rkaudio/。
#
# 用法: ports/rk3562/deploy-vqe.sh [serial]
set -euo pipefail

PKG="${XR_VQE_PKG:?请设置 XR_VQE_PKG 指向 Rockchip VQE 插件包}"
LIB_DIR="${XR_VQE_LIB_DIR:-$PKG/lib/linux-aarch64}"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
CFG="${XR_VQE_CFG:-6mic_2ref}"
SERIAL="${1:-}"
if [ -z "$SERIAL" ]; then
    SERIAL=$(adb devices | awk '/device$/{print $1}' | head -1)
fi
[ -n "$SERIAL" ] || { echo "没有在线 adb 设备" >&2; exit 2; }
[ -d "$PKG" ] || { echo "缺插件包: $PKG" >&2; exit 2; }
[ -d "$LIB_DIR" ] || { echo "缺 so 目录: $LIB_DIR" >&2; exit 2; }

# 算法配置: 6mic+2ref 基线取自插件包(仓库里没有这份, 别用
# board-server/config/config_aivqe_new.json —— 那是 **2mic** 板
# (RK3576/RK3308)的旧文件, 推到这块板会把波束降级成 2mic)。
VQE_CFG_SRC="$PKG/config/$CFG/config_aivqe_new.json"
echo "== 算法配置: $VQE_CFG_SRC"

echo "== 板子: $SERIAL -> $(adb -s "$SERIAL" shell 'cat /proc/device-tree/model' | tr -d '\0')"
echo "== so 来源: $LIB_DIR"

echo "== 1) 推插件模块与依赖库 =="
adb -s "$SERIAL" shell 'mkdir -p /usr/lib/alsa-lib /usr/share/rkaudio'
adb -s "$SERIAL" push "$PKG/lib/linux-aarch64/libasound_module_pcm_rkaudio_vqe.so" /usr/lib/alsa-lib/ >/dev/null
for f in librkaudio_vqe.so librkaudio_common.so librkaudio_nn.so; do
    if [ -f "$LIB_DIR/$f" ]; then
        adb -s "$SERIAL" push "$LIB_DIR/$f" /usr/lib/ >/dev/null
    else
        adb -s "$SERIAL" push "$PKG/lib/linux-aarch64/$f" /usr/lib/ >/dev/null
    fi
done
adb -s "$SERIAL" shell 'chmod 755 /usr/lib/alsa-lib/libasound_module_pcm_rkaudio_vqe.so /usr/lib/librkaudio_*.so'

echo "== 2) 算法配置(AEC + AGC + VQE 内置唤醒) =="
python3 - "$PKG/config/$CFG/config_aivqe_new.json" "$TMP_DIR/vqe.json" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src, encoding="utf-8"))
s = d["skv_configs"]
# 本板要的开关(2026-11-19):
#  aec    enable  6mic+2ref: ref = RK809 播放回采, AEC 的命门
#  agc    enable  PDM 麦电平低, 靠 AGC 拉起来
#  wakeup disable 瑞芯微 VQE 内置唤醒实测 0/10 触发(6mic 波束 + 16ms 周期,
#                   VQE 每帧 16~19ms, 唤醒 FSM 拿不到连续帧) -> 白占 CPU, 关掉。
#                   唤醒判定权在网关侧(RockASR 转写 + 唤醒词门, 实测 10/10);
#                   前端用 sherpa-onnx zipformer KWS 做声学证据。
#                   想试 VQE 唤醒: XR_WAKE_SOURCE=vqe + 这里改回 enable。
#  unkws  disable 任意词表检测要常驻跑 MNN, 白占 CPU, 本板不需要
#  command disable 固定指令词用不上
s["aec"]["status"] = "enable"
s["agc"]["status"] = "enable"
s["wakeup"]["status"] = "disable"
s["unkws"]["status"] = "disable"
s["command"]["status"] = "disable"
json.dump(d, open(dst, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print("  aec=%s agc=%s bf=%s anr=%s wakeup=%s unkws=%s command=%s mic=%s" % (
    s["aec"]["status"], s["agc"]["status"], s["bf"]["status"],
    s["anr"]["status"], s["wakeup"]["status"], s["unkws"]["status"],
    s["command"]["status"], s["bf"]["num_array_mic"]))
PY
adb -s "$SERIAL" push "$TMP_DIR/vqe.json" /usr/share/rkaudio/config_aivqe_new.json >/dev/null
adb -s "$SERIAL" push "$PKG/config/$CFG/wopt_6mic_ring_35mm_8input_8output.bin" /usr/share/rkaudio/ >/dev/null

echo "== 3) 探测设备号并生成 /etc/asound.conf =="
PCM=$(adb -s "$SERIAL" shell 'cat /proc/asound/pcm')
echo "$PCM"
# PCM 行格式: "CC-DD: <cpu dai> ... : <codec dai> ..."
# 取 "CC-DD" 前缀拆出 card/device
parse_pcm() {  # $1=关键字 -> 输出 "card dev"
    local line card dev
    line=$(echo "$PCM" | grep -m1 "$1")
    [ -n "$line" ] || return 1
    card=$(echo "$line" | cut -d: -f1 | cut -d- -f1 | tr -d ' ')
    dev=$(echo "$line" | cut -d: -f1 | cut -d- -f2 | tr -d ' ')
    echo "$card $dev"
}
read PDM_CARD PDM_DEV <<< "$(parse_pcm 'pdm-')"
read SPK_CARD SPK_DEV <<< "$(parse_pcm 'sai-')"
[ -n "$PDM_CARD" ] && [ -n "$PDM_DEV" ] || { echo "没找到 PDM PCM" >&2; exit 2; }
[ -n "$SPK_CARD" ] && [ -n "$SPK_DEV" ] || { echo "没找到 SAI PCM" >&2; exit 2; }
echo "  采集: hw:$PDM_CARD,$PDM_DEV (8ch)   播放: hw:$SPK_CARD,$SPK_DEV (2ch)"

cat > "$TMP_DIR/asound-vqe.conf" <<EOF
# RK3562 EVB2: rkaudio_vqe(6mic+2ref) 采集 + RK809 播放
# 由 ports/rk3562/deploy-vqe.sh 生成, 勿手改
#
# 结构照抄厂商 6mic_2ref 的 asound.conf:
#   pcm_slave.ins  = 8ch 原始 PDM 流(6 阵列麦 + 2 路 RK809 播放回采)
#   pcmin1_dsnoop  = dsnoop 抓这份流
#   rkaudio_vqe_16k= VQE 插件(AEC/波束/降噪/AGC) -> 1ch 16k

pcm_slave.ins {
    pcm "hw:$PDM_CARD,$PDM_DEV"
    channels 8
    rate 16000
    format S16_LE
    period_size 256
    buffer_size 3840
}

pcm.pcmin1_dsnoop {
    type dsnoop
    ipc_key 5678293
    ipc_key_add_uid no
    tstamp_type monotonic
    hw_ptr_alignment auto
    slowptr 0
    slave ins
    bindings.0 0
    bindings.1 1
    bindings.2 2
    bindings.3 3
    bindings.4 4
    bindings.5 5
    bindings.6 6
    bindings.7 7
}

# --- VQE: 6mic+2ref -> AEC/波束/降噪/AGC -> 1ch 16k ---
pcm.rkaudio_vqe_16k {
    type rkaudio_vqe
    slave {
        pcm "pcmin1_dsnoop"
        format S16_LE
        channels 8
        rate 16000
        period_size 256
        period_count 15
    }
    config_file "/usr/share/rkaudio/config_aivqe_new.json"
    output_format S16_LE
    ref_channels 2
    status_file "/tmp/rkaudio_vqe_status"
    dump_pcm 0
    dump_prefix "/tmp/rkaudio_debug"
    dump_path "/tmp/rkaudio_debug"
}

# --- 播放: 前端要 1ch/24k, 板载喇叭走 48k/2ch ---
pcm.rk809_cap {
    type hw
    card $SPK_CARD
    device $SPK_DEV
    format S16_LE
    rate 48000
    channels 2
}

pcm.xr_spk {
    type plug
    slave.pcm rk809_cap
}

# --- 前端采集: VQE 处理后的 1ch/16k ---
# 名字里带 xr_vqe 是必须的: 前端只有看到 "rkaudio_vqe"/"xr_vqe" 才走 VQE 分支
# (那条分支会用 XR_MIC_PERIOD, 且按 partial read 拼帧)。名字不含就走默认分支,
# 固定 period 1600 -> VQE 设备只认 256 -> EINVAL 打不开。
pcm.xr_vqe_16k {
    type plug
    slave.pcm "rkaudio_vqe_16k"
}

pcm.asr_mic {
    type plug
    slave.pcm "xr_vqe_16k"
}

pcm.!default {
    type asym
    capture.pcm {
        type plug
        slave.pcm "rkaudio_vqe_16k"
    }
    playback.pcm {
        type plug
        slave.pcm {
            pcm "xr_spk"
        }
    }
}
EOF
adb -s "$SERIAL" push "$TMP_DIR/asound-vqe.conf" /etc/asound.conf >/dev/null

echo "== 4) ldconfig + 验证虚拟设备 =="
adb -s "$SERIAL" shell 'ldconfig 2>/dev/null; arecord -L 2>/dev/null | grep -iE "rkaudio|asr_mic|xr_spk" | head -8'
echo "== 完成 =="
