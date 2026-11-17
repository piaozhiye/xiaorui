#!/bin/bash
# RK3562 + ES8388 (AIOT LP4X V10): 部署 rkaudio VQE 20261110, 采集变 1 mic + 1 ref。
#
# 硬件前提(已在本板完成):
#   - boot 分区 DTB 的 SAI1 节点带 rockchip,digital-loopback
#   - DLP 模式设成 "2CH: 1 Mic + 1 Loopback" (mixer-es8388.sh 开机自动设)
#     -> 采集 2ch: ch0=板载麦, ch1=板内播放的数字回环参考
#
# 插件包: 设 XR_VQE_PKG 指向 Rockchip 的 rkaudio_vqe_plugin-20261110 目录;
#   2mic 及以下不需要 license.lic。
#
# 板端落点:
#   /usr/lib/alsa-lib/libasound_module_pcm_rkaudio_vqe.so   ALSA 插件
#   /usr/lib/librkaudio_{vqe,common,nn}.so                  插件依赖
#   /usr/share/rkaudio/config_aivqe_new.json                1mic1ref 配置(仓库内)
#   /usr/share/rkaudio/wopt_2mic_linear_60mm_*.bin          波束权重(包内)
#   /usr/share/rkaudio/model_files/unkws_*.bin + keywords.txt (包内)
#   /usr/share/rkaudio/vqefiles/rkaudio_effect_eqdrc_48000hz_2ch.bin
#   /etc/asound.conf                                        VQE 采集 + xr_spk 播放(仓库内)
#   /data/xiaorui/xiaorui.env                               切换到 XR_MIC=xr_vqe_16k
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
PKG="${XR_VQE_PKG:?请设置 XR_VQE_PKG 指向 rkaudio_vqe_plugin-20261110 目录}"
SERIAL="${1:-}"
[ -n "$SERIAL" ] || { echo "用法: $0 SERIAL (必须显式指定, 别自动挑设备)" >&2; exit 2; }
[ -d "$PKG/lib/linux-aarch64" ] || { echo "缺插件包: $PKG" >&2; exit 2; }
ADB="adb -s $SERIAL"

# 覆盖 /etc/asound.conf 与 xiaorui.env 前先备份 (撤销: 从该目录推回原路径)
BACKUP="/tmp/opencode/rk3562-es8388/vqe-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
$ADB pull /etc/asound.conf "$BACKUP/asound.conf" >/dev/null 2>&1 || true
$ADB pull /data/xiaorui/xiaorui.env "$BACKUP/xiaorui.env" >/dev/null 2>&1 || true
echo "覆盖前备份: $BACKUP"

MODEL=$( $ADB shell 'cat /proc/device-tree/model' 2>/dev/null | tr -d '\0' )
case "$MODEL" in *RK3562*) : ;; *) echo "不是 RK3562 ($MODEL)" >&2; exit 1 ;; esac
# 设备树里必须有 DLP 属性, 否则插件收不到 ref, AEC 等于没开
$ADB shell 'ls /proc/device-tree/sai@ff810000/ | grep -q rockchip,digital-loopback' \
    || { echo "SAI1 没有 rockchip,digital-loopback 属性 —— 先给 boot 分区 DTB 的 SAI1 节点加该属性并重启" >&2; exit 1; }

# ---- 1) 插件与依赖 ----
echo "== 1) 推 VQE 插件与依赖 ($PKG) =="
$ADB shell 'mkdir -p /usr/lib/alsa-lib /usr/share/rkaudio/model_files /usr/share/rkaudio/vqefiles'
$ADB push "$PKG/lib/linux-aarch64/libasound_module_pcm_rkaudio_vqe.so" /usr/lib/alsa-lib/ >/dev/null
for f in librkaudio_vqe.so librkaudio_common.so librkaudio_nn.so; do
    [ -f "$PKG/lib/linux-aarch64/$f" ] || { echo "缺 $f" >&2; exit 2; }
    $ADB push "$PKG/lib/linux-aarch64/$f" /usr/lib/ >/dev/null
done
$ADB shell 'chmod 755 /usr/lib/alsa-lib/libasound_module_pcm_rkaudio_vqe.so /usr/lib/librkaudio_*.so; ldconfig 2>/dev/null || true'

# ---- 2) 1mic1ref 配置与模型 ----
echo "== 2) 配置与模型 =="
CFG_SRC="$PKG/config/2mic_2ref/config_aivqe_new1mic1ref.json"
[ -f "$CFG_SRC" ] || CFG_SRC="$DIR/config_aivqe_new1mic1ref.json"
[ -f "$CFG_SRC" ] || { echo "缺 1mic1ref 配置: $CFG_SRC" >&2; exit 2; }
# 关掉 VQE 包自带的 wakeup / UNKWS / command 检测 —— 唤醒由前端 sherpa KWS 做,
# 两套同时开会重复识别、白占 CPU, 而且 VQE 的词表与前端词表不是同一真源。
CFG_TMP=$(mktemp)
python3 "$DIR/patch-vqe-config.py" "$CFG_SRC" "$CFG_TMP" || { rm -f "$CFG_TMP"; exit 1; }
$ADB push "$CFG_TMP" /usr/share/rkaudio/config_aivqe_new.json >/dev/null
rm -f "$CFG_TMP"
WOPT=$(grep -o '/usr/share/rkaudio/wopt_[^"]*\.bin' "$CFG_SRC" | head -1)
[ -n "$WOPT" ] || { echo "配置里没有 wopt 路径" >&2; exit 2; }
$ADB push "$PKG/config/2mic_2ref/$(basename "$WOPT")" "$WOPT" >/dev/null
# UNKWS 已关, 不推它的模型和词表(前端自己的 KWS 词表才是真源)
# EQ/DRC bin: 配置的 EQ 段引用它; 从 Rockchip audio-hal 取
EQ_SRC="${XR_VQE_EQ_BIN:-}"   # 可选: 指向 Rockchip audio-hal 里的 rkaudio_effect_eqdrc_48000hz_2ch.bin
if [ -n "$EQ_SRC" ] && [ -f "$EQ_SRC" ]; then
    $ADB push "$EQ_SRC" /usr/share/rkaudio/vqefiles/ >/dev/null
else
    echo "  提示: 未设置 XR_VQE_EQ_BIN, EQ 段可能报路径错误(不影响 AEC)"
fi

# ---- 3) ALSA 配置(采集走 VQE, 播放仍是 xr_spk) ----
echo "== 3) ALSA 配置 =="
$ADB push "$DIR/asound-vqe.conf" /etc/asound.conf >/dev/null

# ---- 4) 前端切换到 VQE 采集 ----
echo "== 4) 前端采集切到 xr_vqe_16k =="
$ADB push "$DIR/frontend.env.vqe.example" /data/xiaorui/xiaorui.env >/dev/null

# ---- 5) 重启栈并验证 ----
echo "== 5) 重启栈 =="
# 日志是追加的: 必须先记下行号, 只接受**本次启动之后**的新证据, 否则旧轮次的
# "capture xr_vqe_16k" 会让一个没起来的前端也被判成"VQE 链就绪"。
MARK=$($ADB shell 'wc -l < /data/xiaorui/frontend.log 2>/dev/null' 2>/dev/null | tr -dc '0-9')
[ -n "$MARK" ] || { echo "读不到前端日志行数" >&2; exit 1; }
OLD_PID=$($ADB shell 'pgrep -x xiaorui_live | head -1' 2>/dev/null | tr -dc '0-9')
$ADB shell 'pkill -x xiaorui_live 2>/dev/null; sleep 2; bash /etc/init.d/S98xiaorui start' >/dev/null 2>&1
NEW_PID=""
for _ in $(seq 1 30); do
    NEW_PID=$($ADB shell 'pgrep -x xiaorui_live | head -1' 2>/dev/null | tr -dc '0-9')
    [ -n "$NEW_PID" ] && break
    sleep 2
done
[ -n "$NEW_PID" ] || { echo "前端没起来, 查 /data/xiaorui/frontend.log" >&2; exit 1; }
if [ -n "$OLD_PID" ] && [ "$NEW_PID" = "$OLD_PID" ]; then
    echo "前端仍是旧进程(pid=$NEW_PID), 本次启动失败" >&2
    exit 1
fi
$ADB shell "tail -n +$((MARK+1)) /data/xiaorui/frontend.log | grep -q 'capture xr_vqe_16k'" \
    || { echo "本次启动没有 capture xr_vqe_16k, 查 /data/xiaorui/frontend.log" >&2; exit 1; }
echo "VQE 链就绪: 前端 pid=$NEW_PID 新开 capture xr_vqe_16k (1ch/16k)"
echo "验证: adb -s $SERIAL shell 'tail -5 /data/xiaorui/frontend.log'"
