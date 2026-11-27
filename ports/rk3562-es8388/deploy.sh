#!/bin/bash
# RK3562 + ES8388 (AIOT DEMO LP4X V10) 小瑞部署。
#
# 装的东西:
#   /data/miniforge/                 aarch64 Python 3.14(离线: 从本机备份推, 见 README)
#   /data/xiaorui-server/            RKLLM 运行时 + w8a8 模型 + ASR/TTS 模型
#   /data/xiaorui-gateway/           板内网关(代码/配置/venv)
#   /data/xiaorui/                   前端二进制 + KWS 模型 + env
#   /etc/asound.conf                 ES8388 plug fallback / xr_spk
#   /etc/init.d/S98xiaorui           开机自启(定频 + mixer + 板内栈 + 前端)
#   /data/mixer-es8388.sh            板载麦/喇叭 mixer
#
# 特点:
#   - serial 必须显式给, 不自动挑设备
#   - 覆盖板端文件前先备份到本机 /tmp/opencode/rk3562-es8388/backup-<时间戳>/
#   - 不装 rkaudio_vqe / 不用旧板(RK809+PDM)的任何配置
#   - 部署前先跑 check-runtime.sh(ABI + NPU 冒烟), 不过就不装
#
# 用法: bash ports/rk3562-es8388/deploy.sh SERIAL
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
WORK=/tmp/opencode/rk3562-es8388
SERIAL="${1:-}"

if [ -z "$SERIAL" ]; then
    echo "用法: $0 SERIAL" >&2
    echo "提示: adb devices 列出在线设备, 必须显式指定 serial。" >&2
    exit 2
fi
ADB="adb -s $SERIAL"

die() { echo "!! $*" >&2; exit 1; }

# sherpa CPU C API/ONNX Runtime: 默认从本机固定 SHA 的 aarch64 build staging；
# 也可用 XR_SHERPA_LIB_DIR 指向已准备好这两个库的离线 bundle。
if [ -n "${XR_SHERPA_LIB_DIR:-}" ]; then
    SHERPA_LIB_DIR="$XR_SHERPA_LIB_DIR"
    SHERPA_STAGE=0
    SHERPA_CAPI="$SHERPA_LIB_DIR/libsherpa-onnx-c-api.so"
    SHERPA_ORT="$SHERPA_LIB_DIR/libonnxruntime.so"
else
    # 公开仓库不绑定任何个人构建路径: 用 XR_SHERPA_BUILD 指向固定 SHA 的 aarch64 build,
    # 或改用 XR_SHERPA_LIB_DIR 直接给两个 .so 的 bundle。
    [ -n "${XR_SHERPA_BUILD:-}" ] || die "请设置 XR_SHERPA_BUILD 或 XR_SHERPA_LIB_DIR"
    SHERPA_BUILD="$XR_SHERPA_BUILD"
    SHERPA_LIB_DIR="$WORK/sherpa-lib"
    SHERPA_STAGE=1
    SHERPA_CAPI="$SHERPA_BUILD/lib/libsherpa-onnx-c-api.so"
    SHERPA_ORT="$SHERPA_BUILD/_deps/onnxruntime-src/lib/libonnxruntime.so"
fi
LIBGOMP="${XR_LIBGOMP:-}"
[ -n "$LIBGOMP" ] || die "请设置 XR_LIBGOMP 指向 aarch64 的 libgomp.so.1"
VAD_MODEL="${XR_SILERO_VAD_MODEL:-$REPO/models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx}"
MINIFORGE="${XR_MINIFORGE:-$REPO/board-server/miniforge.sh}"
WHEELS="${XR_GATEWAY_WHEELS:-$REPO/board-server/wheels314}"
RKLLM_MODEL="${XR_RKLLM_MODEL:-$REPO/models/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm}"
# UI 必选组件的四个本地源(review Minor-3): 前置检查与安装段共用同一
# 来源, 避免"检查一份路径、安装另一份"漂移后静默重开 P2 矛盾。
UI_BIN="$REPO/ui/build/aarch64/xiaorui_ui"
UI_LIB="$REPO/ui/build/aarch64/lib/liblvgl.so"
UI_RUN="$REPO/ui/run.sh"
UI_INIT="$REPO/ports/rk3562/S99xiaorui-ui"


# 传输断掉时后面的 shell 查询会静默失败, 容易被误判成"板子上没有这些文件"。
# 先确认设备在线, 再谈别的。
ADB_STATE=$($ADB get-state 2>/dev/null | tr -d '\r')
[ "$ADB_STATE" = "device" ] || die "adb 设备不可用 (serial=$SERIAL, state=${ADB_STATE:-无})"

# ---------- 0) 板子身份 ----------
MODEL=$($ADB shell 'cat /proc/device-tree/model' 2>/dev/null | tr -d '\0')
case "$MODEL" in
    *RK3562*) : ;;
    *) die "不是 RK3562 ($MODEL)" ;;
esac
CARDS=$($ADB shell 'cat /proc/asound/cards' 2>/dev/null | tr -d '\0')
case "$CARDS" in
    *rockchipes8388*) : ;;
    *) die "没找到 ES8388 声卡: ${CARDS:-无}" ;;
esac
echo "== 板子: $SERIAL -> $MODEL (ES8388 就位)"

# 不接管有既有 Xiaorui/板内网关数据或音频配置的设备。重复部署只允许覆盖
# 本 profile 自己标记过的目标；避免误覆盖用户已运行的另一个板型/配置。
PROFILE_MARKER=/data/xiaorui/.rk3562-es8388-profile
PROFILE_STATE=/data/xiaorui/.rk3562-es8388-state
PROFILE_OWNED=0
PROFILE_INSTALLED=0
if $ADB shell '[ -e /etc/asound.conf ] || [ -e /etc/init.d/S98xiaorui ] || [ -e /data/xiaorui ] || [ -e /data/xiaorui-server ] || [ -e /data/xiaorui-gateway ]' \
    >/dev/null 2>&1; then
    $ADB shell "test -f $PROFILE_MARKER" >/dev/null 2>&1 \
        || die "板上已有 Xiaorui/网关数据但无 ES8388 profile 标记，拒绝覆盖；先检查并手工备份"
    PROFILE_OWNED=1
    STATE=$($ADB shell "cat $PROFILE_STATE 2>/dev/null" 2>/dev/null | tr -d '\0\r')
    case "$STATE" in
        installed) PROFILE_INSTALLED=1 ;;
        installing) die "上次部署未完成；先用 marker 指向的备份目录运行 rollback.sh" ;;
        *)
            if $ADB shell 'test -x /data/gwvenv/bin/python && test -x /data/xiaorui-gateway/start-board-stack.sh && test -f /data/xiaorui-gateway/data/.config.yaml && test -f /data/xiaorui-server/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm' \
                >/dev/null 2>&1; then
                # 兼容 profile state 文件引入前由本方案首次部署的完整板端。
                PROFILE_INSTALLED=1
            else
                die "profile marker 存在但后端未完整安装；先用备份回滚，不覆盖半成品"
            fi
            ;;
    esac
fi

# ---------- 1) 本机资源检查 ----------
missing=0
for f in \
    "$MINIFORGE" \
    "$WHEELS/requirements-board.lock" \
    "$REPO/board-server/librkllmrt.so" \
    "$RKLLM_MODEL" \
    "$VAD_MODEL" \
    "$REPO/models/sherpa-onnx-sense-voice-zh/model.int8.onnx" \
    "$REPO/tts-matcha/matcha-icefall-zh-baker/model-steps-3.onnx" \
    "$LIBGOMP" \
    "$REPO/ports/rk3562/check-assets.py" \
    "$SHERPA_CAPI" \
    "$SHERPA_ORT" \
    "$REPO/board/xiaorui_live" \
    "$DIR/keywords.txt" \
    "$REPO/models/model-kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" \
    "$REPO/models/model-kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx" \
    "$REPO/models/model-kws/joiner-epoch-12-avg-2-chunk-16-left-64.onnx" \
    "$REPO/models/model-kws/tokens.txt" \
    "$REPO/board-server/cues/xr_ack24k.wav" \
    "$REPO/board-server/cues/xr_hmm24k.wav" \
    "$REPO/board-server/cues/xr_fail24k.wav" \
    "$REPO/board-server/cues/xr_volack24k.wav" \
    "$DIR/asound.conf" "$DIR/frontend.env.example" "$DIR/init-S98xiaorui" \
    "$DIR/mixer-es8388.sh" \
    "$REPO/ports/rk3562/setup-zram.sh" \
    "$DIR/stage-runtime.sh" \
    "$REPO/board-server/probe-port.py" \
    "$DIR/check-runtime.sh" \
    "$DIR/rollback.sh" ; do
    [ -f "$f" ] || { echo "缺资源: $f" >&2; missing=1; }
done
[ "$missing" -eq 0 ] || die "资源不全, 先补齐(见 ports/rk3562/DEPLOYMENT.md)"
[ -d "$REPO/board-server/pkg" ] || die "缺网关代码目录: $REPO/board-server/pkg"
# UI 是本 profile 必选组件(review 2026-11-20): 安装段曾允许"未构建
# 可跳过", 但验收段无条件要求 UI 存在 —— 干净板必然 die 在验收、留下
# 半成品。必须在写板之前就失败并给出构建指引。
for f in "$UI_BIN" "$UI_LIB" "$UI_RUN" "$UI_INIT" \
         "$DIR/11-xiaorui.ini" \
         "$DIR/assets/icon_xiaorui.png" \
         "$DIR/fonts-local.conf" \
         "$DIR/xiaorui-start.sh" "$DIR/xiaorui-stop.sh"; do
    [ -f "$f" ] || die "UI 未构建: 缺 $f — 先跑 ui/build.sh aarch64(本 profile 必选)"
done

# 空间门必须在任何板端写入(包括 marker、探针和 runtime stage)之前完成。
# du 计算随包内容; 首装另预留 Miniforge 解包、Python venv 膨胀及运行余量。
if [ "$SHERPA_STAGE" = "1" ]; then
    SHERPA_DEPLOY_INPUTS=("$SHERPA_CAPI" "$SHERPA_ORT")
else
    SHERPA_DEPLOY_INPUTS=("$SHERPA_LIB_DIR")
fi
if [ "$PROFILE_INSTALLED" = "1" ]; then
    DEPLOY_INPUTS=(
        "$REPO/board/xiaorui_live" "${SHERPA_DEPLOY_INPUTS[@]}"
        "$REPO/models/model-kws" "$REPO/board-server/cues"
        "$DIR/frontend.env.example" "$DIR/asound.conf"
    )
    EXTRA_KB=262144
else
    DEPLOY_INPUTS=(
        "$MINIFORGE" "$WHEELS" "$REPO/board-server/pkg"
        "$REPO/board-server/librkllmrt.so" "$RKLLM_MODEL" "$VAD_MODEL"
        "$REPO/models/sherpa-onnx-sense-voice-zh"
        "$REPO/tts-matcha/matcha-icefall-zh-baker"
        "$REPO/board/xiaorui_live" "${SHERPA_DEPLOY_INPUTS[@]}"
        "$REPO/models/model-kws" "$REPO/board-server/cues" "$LIBGOMP"
    )
    EXTRA_KB=$((1200000 + 700000 + 512000))
fi
if ! ASSET_KB=$(du -sk "${DEPLOY_INPUTS[@]}" 2>/dev/null \
    | awk -v expected="${#DEPLOY_INPUTS[@]}" \
        '{ total += $1; count++ } END { if (count != expected) exit 1; print int(total) }'); then
    die "无法计算部署资源大小"
fi
case "$ASSET_KB" in ''|*[!0-9]*) die "部署资源大小无效" ;; esac
REQUIRED_KB=$((ASSET_KB + EXTRA_KB))
FREE_KB=$($ADB shell 'df -Pk /data' 2>/dev/null \
    | awk 'NR==2 {print $4}' | tr -d '\r ')
case "$FREE_KB" in ''|*[!0-9]*) die "无法读取 /data 可用空间" ;; esac
echo "== 存储预检: 随包资源 ${ASSET_KB}KB + 预留 ${EXTRA_KB}KB; /data 可用 ${FREE_KB}KB =="
[ "$FREE_KB" -ge "$REQUIRED_KB" ] || die "/data 空间不足 (需 ${REQUIRED_KB}KB, 可用 ${FREE_KB}KB)"

# 暂存固定指纹的 sherpa C API/ORT；只用 CPU KWS 构建，不误带 RK3576/RKNN 变体。
mkdir -p "$SHERPA_LIB_DIR"
if [ "$SHERPA_STAGE" = "1" ]; then
    cp "$SHERPA_CAPI" "$SHERPA_LIB_DIR/"
    cp "$SHERPA_ORT" "$SHERPA_LIB_DIR/"
fi
if ! XR_SHERPA_LIB_DIR="$SHERPA_LIB_DIR" \
    python3 "$REPO/ports/rk3562/check-assets.py" --groups frontend; then
    die "sherpa/KWS 资源指纹不匹配"
fi

# ---------- 2) 备份板端将被替换的文件 ----------
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$WORK/backup-$STAMP"
# 时间戳只到秒: 同一秒内再部署一次会复用目录, 把上一次的 manifest 冲掉,
# 那次部署就再也回滚不了。撞名就加后缀。
if [ -e "$BACKUP" ]; then
    BACKUP="$WORK/backup-$STAMP-$$"
fi
mkdir -p "$BACKUP" || die "无法创建备份目录: $BACKUP"
echo "== 备份板端现状 -> $BACKUP"
mkdir -p "$BACKUP/files"
: > "$BACKUP/manifest"
for f in \
    /etc/asound.conf \
    /etc/init.d/S98xiaorui \
    /etc/init.d/S99xiaorui-ui \
    /etc/xdg/weston/weston.ini \
    /data/mixer-es8388.sh \
    /data/setup-zram.sh \
    /data/xiaorui/xiaorui.env \
    /data/xiaorui/start-frontend.sh \
    /data/xiaorui/board-time.sh \
    /data/xiaorui-ui/xiaorui_ui \
    /data/xiaorui-ui/run.sh \
    /data/xiaorui-ui/lib/liblvgl.so \
    /data/xiaorui/.rk3562-es8388-profile \
    /data/xiaorui/.rk3562-es8388-state \
    /data/xiaorui-gateway/probe-port.py \
    /data/xr-es8388-probe.py \
    /data/xiaorui-server/xiaorui_volume \
    /data/xiaorui-server/librkllmrt.so \
    /data/xiaorui-server/lib/libgomp.so.1 \
    /data/xiaorui-gateway/start-board-stack.sh \
    /data/xiaorui-gateway/data/.config.yaml \
    /data/xiaorui/bin/xiaorui_live \
    /data/xiaorui/lib/libsherpa-onnx-c-api.so \
    /data/xiaorui/lib/libonnxruntime.so \
    /data/xiaorui/assets/kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx \
    /data/xiaorui/assets/kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx \
    /data/xiaorui/assets/kws/joiner-epoch-12-avg-2-chunk-16-left-64.onnx \
    /data/xiaorui/assets/kws/tokens.txt \
    /data/xiaorui/assets/kws/keywords.txt \
    /data/xiaorui-server/board-scripts/xr_ack24k.wav \
    /data/xiaorui-server/board-scripts/xr_hmm24k.wav \
    /data/xiaorui-server/board-scripts/xr_fail24k.wav \
    /data/xiaorui-server/board-scripts/xr_volack24k.wav; do
    name=$(printf '%s' "$f" | sed 's|^/||;s|/|__|g')
    if $ADB shell "test -f '$f'" >/dev/null 2>&1; then
        $ADB pull "$f" "$BACKUP/files/$name" >/dev/null 2>&1 \
            || die "备份失败: $f (拒绝继续覆盖)"
        # adb pull/push 不保留权限：把板端权限一起记进清单，回滚时才能还原
        # 执行位(否则 start-board-stack.sh 变不可执行，后续重跑会被判定为半成品)。
        # 权限要完整取: stat 可能给 4 位(如 setuid 的 4755), 截断三位会还原成别的权限。
        mode=$($ADB shell "stat -c '%a' '$f' 2>/dev/null" 2>/dev/null | tr -d '\0\r\n')
        case "$mode" in
            [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) : ;;
            *) mode=644 ;;
        esac
        printf 'PRESENT\t%s\t%s\t%s\n' "$f" "$name" "$mode" >> "$BACKUP/manifest"
    else
        printf 'ABSENT\t%s\t%s\t-\n' "$f" "$name" >> "$BACKUP/manifest"
    fi
done
# Preserve the opus compatibility path as a symlink when that is what existed.
OPUS_LINK=/usr/local/lib/libopus.so.0
OPUS_NAME=$(printf '%s' "$OPUS_LINK" | sed 's|^/||;s|/|__|g')
if $ADB shell "test -L '$OPUS_LINK'" >/dev/null 2>&1; then
    OPUS_TARGET=$($ADB shell "readlink '$OPUS_LINK'" 2>/dev/null | tr -d '\0\r')
    printf 'SYMLINK\t%s\t%s\n' "$OPUS_LINK" "$OPUS_TARGET" >> "$BACKUP/manifest"
elif $ADB shell "test -f '$OPUS_LINK'" >/dev/null 2>&1; then
    $ADB pull "$OPUS_LINK" "$BACKUP/files/$OPUS_NAME" >/dev/null 2>&1 \
        || die "备份失败: $OPUS_LINK (拒绝继续)"
    printf 'PRESENT\t%s\t%s\n' "$OPUS_LINK" "$OPUS_NAME" >> "$BACKUP/manifest"
else
    printf 'ABSENT\t%s\t%s\n' "$OPUS_LINK" "$OPUS_NAME" >> "$BACKUP/manifest"
fi
for d in /data/miniforge /data/gwvenv /data/wheels /data/xiaorui \
         /data/xiaorui-server /data/xiaorui-gateway; do
    if $ADB shell "test -d '$d'" >/dev/null 2>&1; then
        printf 'DIR_PRESENT\t%s\t-\t-\n' "$d" >> "$BACKUP/manifest"
    else
        printf 'DIR_ABSENT\t%s\t-\t-\n' "$d" >> "$BACKUP/manifest"
    fi
done
echo "   备份清单: $BACKUP/manifest"

# mixer 现状也要备份: 本 profile 会把麦 PGA 设成 8、把喇叭 Output 1/2 调到 21,
# 只还原文件不还原 mixer 的话, 回滚后板子的音量/增益仍然是改过的状态。
MIXER_CARD=$($ADB shell 'cat /proc/asound/cards' 2>/dev/null | tr -d '\0' \
    | awk '/^[[:space:]]*[0-9]+ \[/ {print $1; exit}')
: > "$BACKUP/mixer-state.txt"
if [ -n "${MIXER_CARD:-}" ]; then
    for ctl in "Left Channel Capture Volume" "Right Channel Capture Volume" \
               "Output 1 Playback Volume" "Output 2 Playback Volume"; do
        # amixer 的取值有两种形态: 区间控件是 ": values=[27]", 单值控件是 ": values=8";
        # 只认 ": values=" 那一行, 别把 capabilities 行里的 values=1 读进来。
        val=$($ADB shell "amixer -c $MIXER_CARD cget name='$ctl' 2>/dev/null" 2>/dev/null \
            | tr -d '\0' \
            | awk -F'values=' '/^[[:space:]]*: values=/{v=$2; gsub(/[\[\]\[,]+/," ",v); split(v,a,/[^0-9-]+/); print a[1]; exit}')
        [ -n "$val" ] && printf '%s|%s\n' "$ctl" "$val" >> "$BACKUP/mixer-state.txt"
    done
    for sw in "Main Mic" "Speaker" "spk switch"; do
        # 开关型控件只能 sset 位置参数(name= 不认), 状态在 "Playback [on]" 里。
        val=$($ADB shell "amixer -c $MIXER_CARD sget '$sw' 2>/dev/null" 2>/dev/null \
            | tr -d '\0' | sed -n 's/.*\[\(on\|off\)\].*/\1/p' | head -1)
        [ -n "$val" ] && printf '%s|%s\n' "$sw" "$val" >> "$BACKUP/mixer-state.txt"
    done
fi
if [ -s "$BACKUP/mixer-state.txt" ]; then
    echo "   mixer 现状: $BACKUP/mixer-state.txt"
    # 每次部署抓到的都是"当前"值, 而重部署时当前值已经是本 profile 设的 21/8。
    # 回滚要还原的是"装本 profile 之前"的那份, 所以第一次抓到时单独存一份,
    # 之后不再覆盖。
    MIXER_ORIGINAL=/data/xiaorui/.rk3562-es8388-mixer-original
    if ! $ADB shell "test -f $MIXER_ORIGINAL" >/dev/null 2>&1; then
        $ADB push "$BACKUP/mixer-state.txt" "$MIXER_ORIGINAL" >/dev/null 2>&1 \
            && echo "   mixer 初始值已留存: $MIXER_ORIGINAL"
    fi
else
    echo "   mixer 现状: 未读到(回滚时不会改 mixer)"
fi

# 首次部署或恢复中的 profile 标为 installing；只有完整部署通过就绪检查后才标 installed。
# 已 installed 的重部署保持 installed，失败后仍可安全重跑。
$ADB shell 'mkdir -p /data/xiaorui'
$ADB shell "printf '%s\n' '$BACKUP' > $PROFILE_MARKER"
if [ "$PROFILE_INSTALLED" = "1" ]; then
    STATE_TO_WRITE=installed
else
    STATE_TO_WRITE=installing
fi
$ADB shell "printf '%s\n' '$STATE_TO_WRITE' > $PROFILE_STATE"

# 这里不停旧前端: 下面的 ABI/NPU 门没过就退出, 把正在跑的前端杀掉会让板子
# 变成"标记 installed 但没有前端"的半状态。前端在真正重启前一刻才停。

# ---------- 3) ABI/NPU gate 必须先于完整网关/ASR/TTS 部署 ----------
if [ "$PROFILE_INSTALLED" != "1" ]; then
    echo "== Stage Python + RKLLM runtime (不安装网关/ASR/TTS、不起服务) =="
    XR_MINIFORGE="$MINIFORGE" XR_RKLLM_MODEL="$RKLLM_MODEL" XR_LIBGOMP="$LIBGOMP" \
    XR_MINIFORGE_GLIBC_OVERRIDE="${XR_MINIFORGE_GLIBC_OVERRIDE:-2.41}" \
    bash "$DIR/stage-runtime.sh" "$SERIAL" \
        || die "runtime stage 失败；未安装完整网关/ASR/TTS/前端"
else
    echo "== 已安装 profile：保留板内网关/模型，只验证现有运行时 =="
    # 重部署也必须能修运行时: 旧版本板端可能缺 libgomp、旧的 librkllmrt,
    # 或还带着没有 LD_LIBRARY_PATH 修复的 start-board-stack.sh。
    # 这些是 profile 无关的运行时前提, 重推它们不会动模型和网关目录。
    echo "== 重推运行时前提(可自愈旧板端) =="
    $ADB shell 'mkdir -p /data/xiaorui-server/lib'
    $ADB push "$REPO/board-server/librkllmrt.so" /data/xiaorui-server/ >/dev/null \
        || die "重推 librkllmrt.so 失败"
    $ADB push "$LIBGOMP" /data/xiaorui-server/lib/libgomp.so.1 >/dev/null \
        || die "重推 libgomp.so.1 失败"
    $ADB push "$REPO/ports/rk3562/start-board-stack.sh" \
        /data/xiaorui-gateway/start-board-stack.sh >/dev/null \
        || die "重推 start-board-stack.sh 失败"
    $ADB shell 'chmod +x /data/xiaorui-gateway/start-board-stack.sh'
fi

echo "== ABI + 真 NPU 推理冒烟 (必须先通过) =="
if ! bash "$DIR/check-runtime.sh" "$SERIAL"; then
    die "运行时检查未通过；未部署/更新完整网关/ASR/TTS/前端，可运行 rollback.sh"
fi

# 首装时只有在 runtime/NPU gate 通过后才部署 ASR/TTS/网关资产。
# 已安装 profile 的重部署不重写共享后端目录，避免失去可回滚的旧状态。
if [ "$PROFILE_INSTALLED" != "1" ]; then
    echo "== 首装板内网关/ASR/TTS (跳过旧 RK809 init) =="
    XR_SKIP_INIT_INSTALL=1 XR_MINIFORGE="$MINIFORGE" \
    XR_GATEWAY_WHEELS="$WHEELS" XR_RKLLM_MODEL="$RKLLM_MODEL" \
    XR_SILERO_VAD_MODEL="$VAD_MODEL" \
        bash "$REPO/ports/rk3562/deploy-board-gateway.sh" "$SERIAL" \
        || die "板内网关部署失败；manifest/marker 保留，失败后先 rollback"
fi

# ---------- 4) 前端资产 ----------
echo "== 推送前端二进制 + 运行时库 + KWS 模型 =="
$ADB shell 'mkdir -p /data/xiaorui/bin /data/xiaorui/lib /data/xiaorui/assets/kws /data/xiaorui-server/board-scripts' >/dev/null
$ADB push "$REPO/board/xiaorui_live" /data/xiaorui/bin/ >/dev/null || die "推送前端二进制失败"
for lib in libsherpa-onnx-c-api.so libonnxruntime.so; do
    $ADB push "$SHERPA_LIB_DIR/$lib" /data/xiaorui/lib/ >/dev/null || die "推送运行时库失败: $lib"
done
for f in encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx \
         decoder-epoch-12-avg-2-chunk-16-left-64.onnx \
         joiner-epoch-12-avg-2-chunk-16-left-64.onnx tokens.txt; do
    $ADB push "$REPO/models/model-kws/$f" /data/xiaorui/assets/kws/ >/dev/null
done
$ADB push "$DIR/keywords.txt" /data/xiaorui/assets/kws/ >/dev/null
for cue in xr_ack24k xr_hmm24k xr_fail24k xr_volack24k; do
    # 提示音推失败不会让脚本报错, 但前端会少了 ack/fail 音, 所以当场失败。
    $ADB push "$REPO/board-server/cues/$cue.wav" /data/xiaorui-server/board-scripts/ >/dev/null \
        || die "推送提示音失败: $cue.wav"
done
$ADB shell 'echo 100 > /data/xiaorui-server/xiaorui_volume'

# ---------- 5) env / 启动脚本 / ALSA / mixer ----------
echo "== 推送 env / 启动脚本 / ALSA 配置 =="
sed -e "s|^export XR_DEVICE_ID=.*|export XR_DEVICE_ID=xiaorui-rk3562-es8388|" \
    "$DIR/frontend.env.example" > "$WORK/frontend.env"
$ADB push "$WORK/frontend.env" /data/xiaorui/xiaorui.env >/dev/null
$ADB push "$REPO/ports/rk3562/start-frontend.sh" /data/xiaorui/start-frontend.sh >/dev/null
$ADB push "$REPO/ports/rk3562/board-time.sh" /data/xiaorui/board-time.sh >/dev/null
$ADB push "$DIR/mixer-es8388.sh" /data/mixer-es8388.sh >/dev/null
# zram 压缩 swap: 2GB 板子的内存兜底(NPU 模型常驻不可回收, 没它就 OOM 黑屏)
$ADB push "$REPO/ports/rk3562/setup-zram.sh" /data/setup-zram.sh >/dev/null
$ADB shell 'mkdir -p /data/xiaorui-gateway'
$ADB push "$REPO/board-server/probe-port.py" /data/xiaorui-gateway/probe-port.py >/dev/null
# 旧版把探针放在板型专属路径; 统一后清掉它(该文件已进备份清单, 回滚会还原)。
$ADB shell 'rm -f /data/xr-es8388-probe.py'
$ADB push "$DIR/asound.conf" /etc/asound.conf >/dev/null
$ADB push "$DIR/init-S98xiaorui" /etc/init.d/S98xiaorui >/dev/null
# ---------- 6b) weston 竖屏 + 触摸 UI ----------
# 面板原生 1080x1920 竖屏, 但出厂 weston.ini 没 [output] 段, weston 会挑
# 横置 mode -> UI 收到 1920x1080 直接退出 -> 界面全黑(语音不受影响)。
# 见 ports/rk3562-es8388/weston.ini 的说明(完整配置, 覆盖出厂文件)。
$ADB shell 'mkdir -p /etc/xdg/weston'
$ADB push "$DIR/weston.ini" /etc/xdg/weston/weston.ini >/dev/null || die "推送 weston.ini 失败"
# UI 二进制 + LVGL 运行时 + 启动脚本(S99 装好即开机自启)
# 必选(前置资源检查已 die 未构建场景), 不再有"跳过"分支。
$ADB shell 'mkdir -p /data/xiaorui-ui/lib'
$ADB push "$UI_BIN" /data/xiaorui-ui/xiaorui_ui >/dev/null || die "推送 UI 二进制失败"
$ADB push "$UI_LIB" /data/xiaorui-ui/lib/liblvgl.so >/dev/null || die "推送 LVGL 运行时失败"
$ADB push "$UI_RUN" /data/xiaorui-ui/run.sh >/dev/null || die "推送 UI 启动脚本失败"
$ADB push "$UI_INIT" /etc/init.d/S99xiaorui-ui >/dev/null || die "推送 UI init 脚本失败"
$ADB shell 'chmod +x /data/xiaorui-ui/xiaorui_ui /data/xiaorui-ui/run.sh /etc/init.d/S99xiaorui-ui'
# LVGL 运行时依赖 libfreetype.so.6(板上没有就 load 失败, 只有 ui.log 一行)
$ADB shell 'test -f /usr/lib/libfreetype.so.6 || test -f /lib/libfreetype.so.6' \
    >/dev/null 2>&1 || echo "   警告: 板上找不到 libfreetype.so.6, UI 可能加载失败"
# ---------- 6c) 桌面图标: 启动/退出整套小瑞 ----------
# weston-desktop-shell 读 /etc/xdg/weston/weston.ini.d/*.ini 的
# [desktop-launcher]; 厂商的 04 文件不动, 我们另起 11-xiaorui.ini。
$ADB shell 'mkdir -p /etc/xdg/weston/weston.ini.d /usr/share/icons'
$ADB push "$DIR/11-xiaorui.ini" /etc/xdg/weston/weston.ini.d/11-xiaorui.ini >/dev/null \
    || die "推送桌面图标配置失败"
$ADB push "$DIR/assets/icon_xiaorui.png" /usr/share/icons/icon_xiaorui.png >/dev/null \
    || die "推送小瑞图标失败"
$ADB push "$DIR/xiaorui-start.sh" /data/xiaorui-ui/xiaorui-start.sh >/dev/null \
    || die "推送启动脚本失败"
$ADB push "$DIR/xiaorui-stop.sh" /data/xiaorui-ui/xiaorui-stop.sh >/dev/null \
    || die "推送退出脚本失败"
$ADB shell 'chmod +x /data/xiaorui-ui/xiaorui-start.sh /data/xiaorui-ui/xiaorui-stop.sh'
# 桌面图标名要显示中文: weston 走 fontconfig, 默认 sans-serif 是 DejaVu
# (无中文字形 -> "小瑞"显示成豆腐块), 把思源黑体挂上并重建缓存。
$ADB push "$DIR/fonts-local.conf" /etc/fonts/local.conf >/dev/null \
    || die "推送字体配置失败"
$ADB shell 'fc-cache -f >/dev/null 2>&1 || true'
# ★ 推完必须让改动**当场生效**, 否则这次部署等于没做:
#   - weston.ini 的 [output] mode 只有重启 weston 才生效(否则黑屏照旧)
#   - UI 二进制被覆盖后, 跑着的旧进程 /proc/exe 仍匹配(带 " (deleted)"),
#     S99 会认为"已在运行"而不重启 -> 必须显式 restart
# ★ 带超时(真板实测 2026-11-20): S49weston 的 restart 是 killall +
#   `while pgrep -x weston; sleep .1` 无限等, 且 weston 继承 stdout
#   让 adb shell 不返回 —— 不加 timeout 会把整段部署挂死在这里。
timeout 15 $ADB shell '/etc/init.d/S49weston restart' >/dev/null 2>&1 \
    || timeout 15 $ADB shell 'kill $(pgrep -x weston) 2>/dev/null; sleep 2; setsid /usr/bin/weston >/dev/null 2>&1 </dev/null &'
sleep 3
timeout 30 $ADB shell 'sh /etc/init.d/S99xiaorui-ui restart' >/dev/null 2>&1 \
    || echo "   警告: UI 重启失败, 看 /data/xiaorui-ui/ui.log"
sleep 2
# 竖屏 mode 落地校验: DSI-1 面板原生只有 1080x1920, weston 没跑在它上面
# UI 会直接退出(黑屏, 但语音照常 —— 最难查的一类症状)
$ADB shell 'grep -A2 "^\[output\]" /etc/xdg/weston/weston.ini | grep -q "mode=1080x1920"' \
    >/dev/null 2>&1 || die "板端 weston.ini 没有 mode=1080x1920(界面会黑)"

$ADB shell 'chmod +x /data/xiaorui/start-frontend.sh /data/xiaorui/board-time.sh /data/mixer-es8388.sh /data/setup-zram.sh /etc/init.d/S98xiaorui /data/xiaorui-gateway/probe-port.py'
# ---------- 7) 启动 ----------
echo "== 启动全栈 =="
# 推送后逐个核对板端产物: push 失败(空间/只读/传输断)如果不核对,
# 脚本会一路走到"部署完成", 留下一块没有开机自启、没有 env 的半成品板子。
missing_board=0
for f in \
    /etc/asound.conf \
    /etc/init.d/S98xiaorui \
    /etc/init.d/S99xiaorui-ui \
    /etc/xdg/weston/weston.ini \
    /data/mixer-es8388.sh \
    /data/setup-zram.sh \
    /data/xiaorui/xiaorui.env \
    /data/xiaorui/start-frontend.sh \
    /data/xiaorui/board-time.sh \
    /data/xiaorui/bin/xiaorui_live \
    /data/xiaorui-ui/xiaorui_ui \
    /data/xiaorui-ui/run.sh \
    /data/xiaorui-ui/lib/liblvgl.so \
    /data/xiaorui/lib/libsherpa-onnx-c-api.so \
    /data/xiaorui/lib/libonnxruntime.so \
    /data/xiaorui/assets/kws/keywords.txt \
    /data/xiaorui/assets/kws/tokens.txt \
    /data/xiaorui/assets/kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx \
    /data/xiaorui/assets/kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx \
    /data/xiaorui/assets/kws/joiner-epoch-12-avg-2-chunk-16-left-64.onnx \
    /data/xiaorui-server/board-scripts/xr_ack24k.wav \
    /data/xiaorui-server/board-scripts/xr_hmm24k.wav \
    /data/xiaorui-server/board-scripts/xr_fail24k.wav \
    /data/xiaorui-server/board-scripts/xr_volack24k.wav \
    /data/xiaorui-gateway/probe-port.py \
    /data/xiaorui-gateway/data/.config.yaml ; do
    $ADB shell "test -f '$f'" >/dev/null 2>&1 || {
        echo "  板端缺少产物: $f" >&2
        missing_board=1
    }
done
[ "$missing_board" -eq 0 ] || die "板端产物校验失败；profile 保持 installing，可 rollback 后排查"

# 日志行数标记取不到就当失败: 标记变成 0 会让"聆听中"的检查扫到整份历史日志,
# 旧行也能满足检查, 等于没有检查。
# A first install has no frontend.log yet. Create the empty log before taking
# the line marker so the "new frontend started" check remains meaningful.
$ADB shell 'mkdir -p /data/xiaorui; touch /data/xiaorui/frontend.log'
LOG_MARK=$($ADB shell 'wc -l < /data/xiaorui/frontend.log 2>/dev/null' 2>/dev/null | tr -dc '0-9')
[ -n "$LOG_MARK" ] || die "读不到前端日志行数(无法确认新前端是否启动)"
# 记下旧前端 PID: 只看"有没有前端进程"不够, 旧前端反复重启也会满足检查。
OLD_FRONTEND_PID=$($ADB shell 'pgrep -x xiaorui_live | head -1' 2>/dev/null | tr -dc '0-9')
# 先等旧前端真的退出再拉新的: 前端收到 SIGTERM 后要收尾(关声卡)才会消失,
# 旧前端还在时 init 脚本会认为"已经有前端了"而不再启动新的, 结果谁也没在跑。
# sleep 0.5 在 busybox 上可能不支持, 探测一次不行就退化成 sleep 1。
$ADB shell 'pkill -x xiaorui_live 2>/dev/null; i=0; while [ $i -lt 60 ] && pgrep -x xiaorui_live >/dev/null 2>&1; do sleep 0.5 2>/dev/null || sleep 1; i=$((i+1)); done'
$ADB shell 'bash /etc/init.d/S98xiaorui start'
sleep 20
echo "== 前端日志 =="
$ADB shell 'tail -25 /data/xiaorui/frontend.log' | tr -d '\0'
echo "== 板内栈日志 =="
$ADB shell 'tail -8 /data/xiaorui-gateway/data/stack.log 2>/dev/null' | tr -d '\0'
$ADB shell "tail -n +$((LOG_MARK + 1)) /data/xiaorui/frontend.log | grep -q '聆听中'" \
    || die "前端未进入聆听状态；profile 保持 installing，可 rollback 后排查"
# 日志是追加的: 光看"聆听中"这行字不够, 上一轮的旧行也能满足。
# 还要确认进程真的在, 并且是**新**进程(旧 PID 没被复用)。
NEW_FRONTEND_PID=$($ADB shell 'pgrep -x xiaorui_live | head -1' 2>/dev/null | tr -dc '0-9')
[ -n "$NEW_FRONTEND_PID" ] \
    || die "前端未运行(进程不存在)；profile 保持 installing，可 rollback 后排查"
if [ -n "$OLD_FRONTEND_PID" ] && [ "$NEW_FRONTEND_PID" = "$OLD_FRONTEND_PID" ]; then
    die "前端仍是旧进程(pid=$NEW_FRONTEND_PID)，新前端没起来；profile 保持 installing，可 rollback 后排查"
fi

# I5: mixer 必须真的被设成 profile 的值, 否则唤醒词可能永远听不到。
MIXER_CARD=$($ADB shell 'cat /proc/asound/cards' 2>/dev/null | tr -d '\0' \
    | awk '/^[[:space:]]*[0-9]+ \[/ {print $1; exit}')
if [ -n "${MIXER_CARD:-}" ]; then
    PGA=$($ADB shell "amixer -c $MIXER_CARD cget name='Left Channel Capture Volume' 2>/dev/null" \
        2>/dev/null | tr -d '\0' \
        | awk -F'values=' '/^[[:space:]]*: values=/{v=$2; gsub(/[\[\]\[,]+/," ",v); split(v,a,/[^0-9-]+/); print a[1]; exit}')
    OUT=$($ADB shell "amixer -c $MIXER_CARD cget name='Output 1 Playback Volume' 2>/dev/null" \
        2>/dev/null | tr -d '\0' \
        | awk -F'values=' '/^[[:space:]]*: values=/{v=$2; gsub(/[\[\]\[,]+/," ",v); split(v,a,/[^0-9-]+/); print a[1]; exit}')
    [ "${PGA:-}" = "8" ] \
        || die "麦 PGA 没设成 8(实际 ${PGA:-无}), 唤醒词可能听不到；profile 保持 installing，可 rollback 后排查"
    [ "${OUT:-}" = "15" ] \
        || die "喇叭 Output 1 没设成 15(实际 ${OUT:-无}), 音量不是 -16.5dB 档；profile 保持 installing，可 rollback 后排查"
    echo "  mixer 复核: PGA=$PGA, Output 1=$OUT"
else
    die "找不到声卡号，无法复核 mixer"
fi
$ADB shell "/data/miniforge/bin/python3.14 /data/xiaorui-gateway/probe-port.py 8001" \
    || die "板内网关 8001 未就绪；profile 保持 installing，可 rollback 后排查"
$ADB shell "printf '%s\n' installed > $PROFILE_STATE"
echo
echo "部署完成。备份在: $BACKUP"
echo "回滚: bash $DIR/rollback.sh $SERIAL $BACKUP"
