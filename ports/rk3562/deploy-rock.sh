#!/bin/bash
# RK3562: 部署 Rockchip RockASR / RockTTS (板内 NPU 的 ASR/TTS).
#
# 板型无关(RK809 / ES8388 都一样), 但 SDK 的 .so 与模型是按 RK356x 编译的。
#
# 资源不入 git, 从本机解压好的 SDK 目录读:
#   XR_ROCK_ASR_SDK  rockasr_android_linux_rk356x_* 解压目录
#   XR_ROCK_TTS_SDK  Rockchip_RockTTSV1_SDK_Release_* 解压目录
#   XR_ROCK_LIC_DIR  放 key_asr.lic + key_tts.lic 的目录(设备绑定, 不进 git)
#
# 板端布局:
#   /data/xiaorui-server/rockasr/{lib,data,key_asr.lic,rockx_auth_config.json}
#   /data/xiaorui-server/rocktts/{lib,data,key_tts.lic,rockx_auth_config.json}
#
# 注意 1: TTS 模型必须展平到 data/models/ 下(去掉 zip 里的 rk356x 层级),
#   否则 rockx2 子图 init 静默失败、运行期空指针崩溃(2026-11-18 实测,
#   官方 demo 也一样崩)。
# 注意 2: 装完必须让板上的 zram 生效(ports/rk3562/setup-zram.sh)。
#   三个 NPU 模型同时驻留要 ~1700MB/1963MB, 没 zram 兜底会 OOM 黑屏。
# 注意 3: 授权是设备绑定的, license 必须在**这台板子**上用 rkauth 工具生成
#   (PC 侧: rkdevice_info -> device.inf -> rkauth_tool_bin -d device.inf -m asr|tts)。
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERIAL="${1:-}"
[ -n "$SERIAL" ] || { echo "用法: deploy-rock.sh <serial>  (需 XR_ROCK_ASR_SDK/XR_ROCK_TTS_SDK/XR_ROCK_LIC_DIR)" >&2; exit 2; }

ASR_SDK="${XR_ROCK_ASR_SDK:?需要 XR_ROCK_ASR_SDK 指向 rockasr 解压目录}"
TTS_SDK="${XR_ROCK_TTS_SDK:?需要 XR_ROCK_TTS_SDK 指向 RockTTS 解压目录}"
LIC_DIR="${XR_ROCK_LIC_DIR:?需要 XR_ROCK_LIC_DIR 指向 license 目录}"

ADB="adb -s $SERIAL"

# ---------- 1) 本机资源守卫 ----------
for f in \
    "$ASR_SDK/lib/Linux/aarch64/librockasr.so" \
    "$ASR_SDK/lib/Linux/aarch64/libonnxruntime.so" \
    "$ASR_SDK/rockx2/lib/Linux/aarch64/librknnrt.so" \
    "$ASR_SDK/rockx2/lib/Linux/aarch64/librockx2.so" \
    "$ASR_SDK/rockx2/lib/Linux/aarch64/librockx_modules.so" \
    "$ASR_SDK/data/rk3562/asr.data" \
    "$ASR_SDK/data/rk3562/bpe.model" \
    "$ASR_SDK/data/rk3562/tokens.txt" \
    "$ASR_SDK/data/rk3562/embedding_matrix.txt" \
    "$TTS_SDK/lib/Linux/aarch64/librocktts.so" \
    "$TTS_SDK/rockx2/lib/Linux/aarch64/librknnrt.so" \
    "$TTS_SDK/rockx2/lib/Linux/aarch64/librockx2.so" \
    "$TTS_SDK/rockx2/lib/Linux/aarch64/librockx_modules.so" \
    "$TTS_SDK/data/frontend_dicts" \
    "$LIC_DIR/key_asr.lic" \
    "$LIC_DIR/key_tts.lic" ; do
    [ -e "$f" ] || { echo "缺资源: $f" >&2; exit 2; }
done
for f in am_part1.data am_part2.data voc.data; do
    [ -f "$TTS_SDK/data/models/rk356x/$f" ] || {
        echo "缺 TTS 模型: $TTS_SDK/data/models/rk356x/$f" >&2; exit 2; }
done

# ---------- 2) 板子守卫 ----------
MODEL=$($ADB shell 'cat /proc/device-tree/model' | tr -d '\0')
case "$MODEL" in *RK3562*) : ;; *) echo "不是 RK3562 ($MODEL)" >&2; exit 1 ;; esac
FREE_KB=$($ADB shell 'df -Pk /data' | awk 'NR==2{print $4}' | tr -d '\r')
# 资源 ~660MB + 运行余量
[ "$FREE_KB" -ge $((700*1024)) ] || { echo "/data 空间不足 (需 ~700MB, 可用 $((FREE_KB/1024))MB)" >&2; exit 1; }
echo "== 板子: $SERIAL ($MODEL), /data 可用 $((FREE_KB/1024))MB =="

# 禁止向正在使用的 SDK 覆盖文件，也禁止探针与重服务双起。
if $ADB shell 'pgrep -f "llm_rkll[m].py|server/[a]pp.py|rock/[a]sr_helper.py|rock/[t]ts_helper.py" >/dev/null'; then
    echo "请先停止板内全栈，再部署 Rock SDK（自检会加载模型）" >&2
    exit 1
fi
$ADB shell 'sh /data/setup-zram.sh' || exit 1

# ---------- 3) 备份旧 auth json(如有) ----------
STAMP=$(date +%Y%m%d-%H%M%S)
for d in rockasr rocktts; do
    $ADB shell "test -f /data/xiaorui-server/$d/rockx_auth_config.json" \
        && $ADB shell "cp /data/xiaorui-server/$d/rockx_auth_config.json /data/xiaorui-server/$d/rockx_auth_config.json.bak-$STAMP" \
        && echo "备份 $d/rockx_auth_config.json.bak-$STAMP"
done

# ---------- 4) 推送 ----------
echo "== 推 RockASR =="
$ADB shell 'mkdir -p /data/xiaorui-server/rockasr/lib /data/xiaorui-server/rockasr/data'
$ADB push "$ASR_SDK/lib/Linux/aarch64/librockasr.so" \
          "$ASR_SDK/lib/Linux/aarch64/libonnxruntime.so" \
          "$ASR_SDK/rockx2/lib/Linux/aarch64/librknnrt.so" \
          "$ASR_SDK/rockx2/lib/Linux/aarch64/librockx2.so" \
          "$ASR_SDK/rockx2/lib/Linux/aarch64/librockx_modules.so" \
          /data/xiaorui-server/rockasr/lib/ >/dev/null || exit 1
$ADB push "$ASR_SDK/data/rk3562/asr.data" "$ASR_SDK/data/rk3562/bpe.model" \
          "$ASR_SDK/data/rk3562/tokens.txt" "$ASR_SDK/data/rk3562/embedding_matrix.txt" \
          /data/xiaorui-server/rockasr/data/ >/dev/null || exit 1
$ADB push "$LIC_DIR/key_asr.lic" /data/xiaorui-server/rockasr/ >/dev/null || exit 1

echo "== 推 RockTTS =="
$ADB shell 'mkdir -p /data/xiaorui-server/rocktts/lib /data/xiaorui-server/rocktts/data/models'
$ADB push "$TTS_SDK/lib/Linux/aarch64/librocktts.so" \
          "$TTS_SDK/rockx2/lib/Linux/aarch64/librknnrt.so" \
          "$TTS_SDK/rockx2/lib/Linux/aarch64/librockx2.so" \
          "$TTS_SDK/rockx2/lib/Linux/aarch64/librockx_modules.so" \
          /data/xiaorui-server/rocktts/lib/ >/dev/null || exit 1
$ADB push "$TTS_SDK/data/frontend_dicts" /data/xiaorui-server/rocktts/data/ >/dev/null || exit 1
# 关键: 展平 rk356x 层级
for f in am_part1.data am_part2.data voc.data; do
    $ADB push "$TTS_SDK/data/models/rk356x/$f" \
              /data/xiaorui-server/rocktts/data/models/ >/dev/null || exit 1
done
$ADB push "$LIC_DIR/key_tts.lic" /data/xiaorui-server/rocktts/ >/dev/null || exit 1

# ---------- 5) 生成授权配置 ----------
$ADB shell 'cat > /data/xiaorui-server/rockasr/rockx_auth_config.json <<EOF
{
    "trial": 1,
    "rkauth_modules_config": [
        {
            "module": "asr",
            "license_path": "/data/xiaorui-server/rockasr/key_asr.lic"
        }
    ]
}
EOF
cat > /data/xiaorui-server/rocktts/rockx_auth_config.json <<EOF
{
    "trial": 1,
    "rkauth_modules_config": [
        {
            "module": "tts",
            "license_path": "/data/xiaorui-server/rocktts/key_tts.lic"
        }
    ]
}
EOF
echo "auth json 已生成"'

# ---------- 6) 自检: 拉起两个 helper 验证 license+模型+so ----------
echo "== 自检: 拉起 helper 验证初始化 =="
$ADB push "$DIR/rock-selftest.py" /data/xiaorui-server/ >/dev/null || exit 1
if $ADB shell 'timeout 90 /data/gwvenv/bin/python /data/xiaorui-server/rock-selftest.py' 2>&1 | tail -8; then
    echo "== 部署完成: /data/xiaorui-server/rockasr + rocktts =="
else
    echo "== 自检失败: 检查 license 是否本机生成(设备绑定)、SDK 版本是否匹配 ==" >&2
    exit 1
fi
