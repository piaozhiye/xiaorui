#!/bin/bash
# PC 侧: 把小瑞网关栈部署到 RK3562 板子(板内全离线形态)
#
# 装到板上的东西:
#   /data/miniforge/                 aarch64 Python 3.14 (取自 3576 备份的 miniforge.sh)
#   /data/gwvenv/                    网关依赖 venv (仓内 board-server/wheels314 + PyPI sherpa_onnx aarch64)
#   /data/xiaorui-gateway/server/     网关代码 (board-server/pkg)
#   /data/xiaorui-gateway/models/     snakers4_silero-vad
#   /data/xiaorui-gateway/data/.config.yaml   本目录 gateway-board.yaml
#   /data/xiaorui-server/asr-sense-voice/     SenseVoice ASR 模型
#   /data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker/  Matcha TTS 模型
#   /data/xiaorui-server/librkllmrt.so         RKLLM 运行时(支持 RK3562)
#   /data/xiaorui-server/llm_rkllm.py          板内大模型服务(OpenAI 兼容, :8112)
#
# 用法: ports/rk3562/deploy-board-gateway.sh [serial]
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
WHEELS="${XR_GATEWAY_WHEELS:-$REPO/board-server/wheels314}"
VAD_MODEL="${XR_SILERO_VAD_MODEL:-$REPO/models/snakers4_silero-vad/src/silero_vad/data/silero_vad.onnx}"
RKLLM="${XR_RKLLM_MODEL:-$REPO/models/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm}"
PORT_PROBE="$REPO/board-server/probe-port.py"
for asset in "$REPO/board-server/librkllmrt.so" "$PORT_PROBE" "$VAD_MODEL" "$RKLLM" \
    "$DIR/setup-zram.sh" "$DIR/init-S98xiaorui"; do
    [ -f "$asset" ] || { echo "缺部署资源: $asset，见 DEPLOYMENT.md" >&2; exit 2; }
done
[ -d "$WHEELS" ] || { echo "缺离线 wheel 目录: $WHEELS" >&2; exit 2; }
for asset in "$REPO/models/sherpa-onnx-sense-voice-zh" \
    "$REPO/tts-matcha/matcha-icefall-zh-baker"; do
    [ -d "$asset" ] || { echo "缺模型目录: $asset" >&2; exit 2; }
done
python3 "$DIR/render-config.py" --output-dir "$TMP_DIR/gateway"
python3 "$DIR/prepare-wheels.py" --directory "$WHEELS"
SERIAL="${1:-}"
if [ -z "$SERIAL" ]; then
    SERIAL=$(adb devices | awk '/device$/{print $1}' | head -1)
fi
[ -n "$SERIAL" ] || { echo "没有在线 adb 设备" >&2; exit 2; }
MODEL=$(adb -s "$SERIAL" shell 'cat /proc/device-tree/model' | tr -d '\0')
case "$MODEL" in *RK3562*) : ;; *) echo "不是 RK3562 ($MODEL)" >&2; exit 1 ;; esac
echo "== 板子: $SERIAL -> $MODEL"

echo "== 1) Python(miniforge aarch64) =="
if ! adb -s "$SERIAL" shell '/data/miniforge/bin/python3 -V' 2>/dev/null | grep -q Python; then
    MINIFORGE="${XR_MINIFORGE:-$REPO/board-server/miniforge.sh}"
    [ -f "$MINIFORGE" ] || { echo "缺 miniforge 安装器: $MINIFORGE" >&2; exit 2; }
    adb -s "$SERIAL" push "$MINIFORGE" /data/miniforge.sh >/dev/null
    # 板端没有 ldd, 安装器探测 glibc 会误判 -> 用 CONDA_OVERRIDE_GLIBC 覆盖
    # (板端是 Buildroot glibc 2.41)
    adb -s "$SERIAL" shell 'cd /data && CONDA_OVERRIDE_GLIBC=2.41 sh ./miniforge.sh -b -p /data/miniforge' >/dev/null
    adb -s "$SERIAL" shell 'rm -f /data/miniforge.sh' || true
fi
adb -s "$SERIAL" shell '/data/miniforge/bin/python3 -V'

echo "== 2) 网关依赖(离线 wheel) =="
adb -s "$SERIAL" shell 'mkdir -p /data/wheels' >/dev/null
adb -s "$SERIAL" push "$WHEELS/." /data/wheels/ >/dev/null
# opuslib_next 用 ctypes.util.find_library 找 libopus: 板端没有 ldconfig/gcc,
# 它的 Linux fallback 只认 x86_64 路径 -> 造个 /usr/local/lib/libopus.so.0 软链
adb -s "$SERIAL" shell 'mkdir -p /usr/local/lib; if [ ! -e /usr/local/lib/libopus.so.0 ] && [ ! -L /usr/local/lib/libopus.so.0 ]; then ln -s /usr/lib/libopus.so.0 /usr/local/lib/libopus.so.0; fi'
if ! adb -s "$SERIAL" shell '[ -x /data/gwvenv/bin/python ]' 2>/dev/null; then
    adb -s "$SERIAL" shell '/data/miniforge/bin/python3 -m venv /data/gwvenv'
fi
adb -s "$SERIAL" shell '/data/gwvenv/bin/python -c "import sys; assert sys.version_info[:2] == (3, 14)"'
adb -s "$SERIAL" shell '/data/gwvenv/bin/pip install -q --no-index --no-deps --require-hashes \
    --find-links=/data/wheels -r /data/wheels/requirements-board.lock' >/dev/null
adb -s "$SERIAL" shell '/data/gwvenv/bin/python -c "import numpy,sherpa_onnx,onnxruntime,openai,opuslib_next;print(\"deps ok\")"'

echo "== 3) 网关代码 + 模型 =="
adb -s "$SERIAL" shell 'mkdir -p /data/xiaorui-gateway/{server,work/tmp,data,models} /data/xiaorui-server' >/dev/null
adb -s "$SERIAL" push "$TMP_DIR/gateway/server/." /data/xiaorui-gateway/server/ >/dev/null
# 网关配置: 把板内形态的基线配置和"唤醒词表真源"合成一份再推。
# 唤醒词表只认 board-server/config/wakeup_words.xiaorui.yaml 这一个真源 ——
# 手抄副本一定漂移(2026-11-11: 板上的 .config.yaml 压根没有 wakeup_words,
# 唤醒词门只剩"小X小Y"形状兜底)。
adb -s "$SERIAL" push "$TMP_DIR/gateway/data/.config.yaml" /data/xiaorui-gateway/data/.config.yaml >/dev/null
# 推完立刻验: 板上配置必须真的有 wakeup_words(漏了要当场炸, 别等运行时才发现)
if ! adb -s "$SERIAL" shell 'grep -c "^wakeup_words:" /data/xiaorui-gateway/data/.config.yaml' | grep -qx 1; then
    echo "板上 .config.yaml 缺 wakeup_words" >&2
    exit 1
fi
adb -s "$SERIAL" push "$REPO/board-server/librkllmrt.so" /data/xiaorui-server/ >/dev/null
adb -s "$SERIAL" push "$REPO/board-server/llm_rkllm.py" /data/xiaorui-server/ >/dev/null

echo "== 4) ASR/TTS/VAD 模型 =="
adb -s "$SERIAL" shell 'mkdir -p /data/xiaorui-server/asr-sense-voice /data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker /data/xiaorui-gateway/models/snakers4_silero-vad/src/silero_vad/data' >/dev/null
adb -s "$SERIAL" push "$REPO/models/sherpa-onnx-sense-voice-zh/." /data/xiaorui-server/asr-sense-voice/ >/dev/null
adb -s "$SERIAL" push "$REPO/tts-matcha/matcha-icefall-zh-baker/." /data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker/ >/dev/null
adb -s "$SERIAL" push "$VAD_MODEL" \
    /data/xiaorui-gateway/models/snakers4_silero-vad/src/silero_vad/data/ >/dev/null

echo "== 5) 板内大模型 =="
adb -s "$SERIAL" push "$RKLLM" \
    /data/xiaorui-server/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm >/dev/null

echo "== 6) 启动脚本 + 开机自启 =="
adb -s "$SERIAL" push "$DIR/start-board-stack.sh" /data/xiaorui-gateway/start-board-stack.sh >/dev/null
# zram 压缩 swap: 2GB 板子的内存兜底。板子出厂 SwapTotal=0, NPU 模型常驻
# 不可回收(RockASR 558MB + RockTTS 229MB + RKLLM 604MB), 余量不够时内核
# OOM 会干掉 init -> 整机黑屏(2026-11-18 实测三次)。必须独立于 HA 装。
adb -s "$SERIAL" push "$DIR/setup-zram.sh" /data/setup-zram.sh >/dev/null
adb -s "$SERIAL" shell 'chmod +x /data/setup-zram.sh'
if [ "${XR_SKIP_INIT_INSTALL:-0}" = "1" ]; then
    # ES8388 profile 自带开机自启脚本(控制项与 RK809 完全不同), 不装旧板的 S98。
    adb -s "$SERIAL" shell 'chmod +x /data/xiaorui-gateway/start-board-stack.sh'
else
    adb -s "$SERIAL" push "$DIR/init-S98xiaorui" /etc/init.d/S98xiaorui >/dev/null
    adb -s "$SERIAL" shell 'chmod +x /data/xiaorui-gateway/start-board-stack.sh /etc/init.d/S98xiaorui'
fi

echo "== 7) 启动板内栈 =="
# start-board-stack.sh requires this file; push before starting it.
adb -s "$SERIAL" shell 'mkdir -p /data/xiaorui-gateway'
adb -s "$SERIAL" push "$PORT_PROBE" /data/xiaorui-gateway/probe-port.py >/dev/null
adb -s "$SERIAL" shell 'chmod +x /data/xiaorui-gateway/probe-port.py'
adb -s "$SERIAL" shell 'pkill -f "[s]erver/app.py" 2>/dev/null; sleep 1'
adb -s "$SERIAL" shell '(setsid sh /data/xiaorui-gateway/start-board-stack.sh >>/data/xiaorui-gateway/data/stack.log 2>&1 </dev/null &); true'
probe_board_port() {
    adb -s "$SERIAL" shell "/data/miniforge/bin/python3.14 /data/xiaorui-gateway/probe-port.py $1" \
        >/dev/null 2>&1
}
for port in 8112 8001 8004; do
    elapsed=0
    until probe_board_port "$port"; do
        [ "$elapsed" -lt 180 ] || {
            echo "端口 $port 未就绪(180s)，查 /data/xiaorui-gateway/data/stack.log" >&2
            exit 1
        }
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "$port OK"
done

# ---------- 8) Rock ASR/TTS (可选) ----------
# Rockchip 官方 NPU 引擎, 替换板内的 SenseVoice/Matcha:
#   识别 1.4s(开源 3s), 合成 0.43s(开源 1~2s), 而且网关 RSS 从 786MB 降到 41MB。
# SDK 二进制和模型不入 git(~650MB), 本机解压好再指过来:
#   XR_ROCK_ASR_SDK / XR_ROCK_TTS_SDK / XR_ROCK_LIC_DIR
if [ -n "${XR_ROCK_ASR_SDK:-}" ]; then
    echo "== 部署 Rock ASR/TTS (NPU) =="
    bash "$DIR/deploy-rock.sh" "$SERIAL" || {
        echo "Rock 部署失败, 保持现有引擎" >&2
        exit 1
    }
    # 引擎换了, 配置里的 selected_module 要跟着切
    adb -s "$SERIAL" shell 'sed -i "s/^  ASR: LocalSenseVoice$/  ASR: RockASR/; s/^  TTS: SherpaLocal$/  TTS: RockTTS/" /data/xiaorui-gateway/data/.config.yaml' \
        || true
    echo "== 重启网关让新引擎生效 =="
    adb -s "$SERIAL" shell 'pkill -f "server/[a]pp.py" 2>/dev/null; sleep 2; (setsid sh /data/xiaorui-gateway/start-board-stack.sh >>/data/xiaorui-gateway/data/stack.log 2>&1 </dev/null &); true'
    for port in 8001 8004; do
        elapsed=0
        until probe_board_port "$port"; do
            [ "$elapsed" -lt 120 ] || {
                echo "端口 $port 未就绪(120s)" >&2
                exit 1
            }
            sleep 3
            elapsed=$((elapsed + 3))
        done
        echo "$port OK"
    done
else
    echo "== 未设 XR_ROCK_ASR_SDK, 保持板内 SenseVoice/Matcha =="
fi
