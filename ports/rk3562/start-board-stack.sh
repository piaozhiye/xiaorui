#!/bin/bash
# RK3562 板内全离线栈: RKLLM(本地 NPU 大模型, :8112) + 网关(:8001/:8004)
#
# 注意: 板端 busybox 的 `ss -tln` 不显示监听端口(实测输出为空),
# 所以就绪判定一律用 /dev/tcp 探测, 不用 ss。
#
# 板上资源:
#   /data/xiaorui-server/librkllmrt.so                     RKLLM 运行时(支持 RK3562)
#   /data/xiaorui-server/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm  板内 NPU 模型件
#   /data/xiaorui-server/llm_rkllm.py                      OpenAI 兼容 SSE 服务
#   /data/xiaorui-server/asr-sense-voice/                  SenseVoice ASR 模型
#   /data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker/  Matcha TTS 模型
#   /data/xiaorui-gateway/server/                          网关代码
#
# 用法: sh /data/xiaorui-gateway/start-board-stack.sh
set -u

GW=/data/xiaorui-gateway
SRV=/data/xiaorui-server
PY=/data/gwvenv/bin/python
MODEL=${XR_RKLLM_MODEL:-$SRV/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm}
# 人设里写明跑在 RK3562 的 NPU 上(llm_rkllm.py 默认写的是 RK3576)
export XR_RKLLM_PERSONA="${XR_RKLLM_PERSONA:-你是小瑞，运行在 RK3562 开发板上的端侧语音助手，大脑在本机 NPU 上本地推理。回答控制在两三句话内，适合语音播报，不要用列表。}"

log() { echo "[board-stack $(date +%H:%M:%S)] $*"; }

mkdir -p "$GW/work" "$GW/data" "$GW/work/tmp"
ln -sfn "$GW/server/core" "$GW/work/core"
ln -sfn "$GW/models" "$GW/work/models"

# 网关配置目录必须显式指定: 默认会去找 <pkg>/data/.config.yaml(板上是空的),
# 结果整份默认配置生效(ASR=FunASR / LLM=ChatGLM / TTS=EdgeTTS)。
export XR_DATA_DIR="$GW/data"
export XR_CONFIG_FILE="$GW/data/.config.yaml"

# 端口探测: 板端 busybox 的 ss 不可用, 用 bash 的 /dev/tcp
probe() { (echo > /dev/tcp/127.0.0.1/"$1") 2>/dev/null; }

# --- 1) RKLLM 大模型 (NPU) ---
if ! probe 8112; then
    log "拉起 RKLLM 大模型 (加载 20-40 s)"
    setsid "$PY" "$SRV/llm_rkllm.py" --model "$MODEL" \
        >>"$GW/data/rkllm.log" 2>&1 </dev/null &
    i=0
    while [ $i -lt 60 ]; do
        probe 8112 && break
        sleep 2
        i=$((i + 1))
    done
    probe 8112 \
        && log "RKLLM 就绪 :8112" || { log "RKLLM 未就绪, 看 $GW/data/rkllm.log"; exit 1; }
else
    log "RKLLM 已在跑"
fi

# --- 2) 网关 ---
if ! probe 8001; then
    log "拉起网关 :8001"
    cd "$GW/work" || exit 1
    PYTHONPATH="$GW/server" setsid "$PY" "$GW/server/app.py" \
        >>"$GW/data/gateway.log" 2>&1 </dev/null &
    i=0
    while [ $i -lt 45 ]; do
        probe 8001 && probe 8004 && break
        sleep 2
        i=$((i + 1))
    done
    probe 8001 && probe 8004 \
        && log "网关就绪 :8001" || { log "网关未就绪, 看 $GW/data/gateway.log"; exit 1; }
else
    probe 8004 || { log "网关 8001 在监听但 8004 未就绪"; exit 1; }
    log "网关已在跑"
fi

log "板内栈就绪: 前端连 ws://127.0.0.1:8001/xiaorui/v1/"
