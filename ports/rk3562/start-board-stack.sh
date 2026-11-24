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
# librkllmrt.so 的 NEEDED 里有 libgomp.so.1, 部分板端镜像(比如 RK3562 AIOT
# LP4X V10 / Buildroot 2026.02) 没自带, 要用 /data/xiaorui-server/lib 里那份。
# 目录不存在时这个赋值无害(动态链接器会忽略不存在的路径)。
export LD_LIBRARY_PATH="$SRV/lib:${LD_LIBRARY_PATH:-}"
# 同一 NPU 上互斥推理，避免 RKLLM 与 RockTTS 重叠时产生沙哑的原始音频。
export XR_NPU_LOCK_FILE="${XR_NPU_LOCK_FILE:-/tmp/xiaorui-npu.lock}"
# 协作式调度(2026-11-19): TTS 请求 NPU 时, RKLLM 在 token 边界主动让出 NPU,
# TTS 合成完再续写。避免"LLM 整轮生成完(35s) TTS 才能开始"的长等待。
export XR_NPU_COOPERATIVE="${XR_NPU_COOPERATIVE:-1}"
# 在线音乐/电台: ffmpeg 解 HTTP 直链(mp3/aac/flac/opus -> 16k mono s16le),
# 装法见 ports/rk3562/deploy-music.sh。目录不存在时这个赋值无害,
# music_stream.find_ffmpeg 会继续找 /usr/bin/ffmpeg 等候选。
export XR_FFMPEG="${XR_FFMPEG:-/data/xiaorui-gateway/deps/ffmpeg-min/ffmpeg}"
# 人设里写明跑在 RK3562 的 NPU 上(llm_rkllm.py 默认写的是 RK3576)
export XR_RKLLM_PERSONA="${XR_RKLLM_PERSONA:-你是小瑞，运行在 RK3562 开发板上的端侧语音助手，大脑在本机 NPU 上本地推理。回答控制在两三句话内，适合语音播报，不要用列表。}"

log() { echo "[board-stack $(date +%H:%M:%S)] $*"; }

mkdir -p "$GW/work" "$GW/data" "$GW/work/tmp"
ln -sfn "$GW/server/core" "$GW/work/core"
ln -sfn "$GW/models" "$GW/work/models"
# 音乐: 离线库与在线歌单。play_music 插件按 music_dir 的同级目录推歌单路径,
# 两处软链都指向 server/ 同一层, 免得"电台有目录没歌单"。
ln -sfn "$GW/server/music" "$GW/work/music"
ln -sfn "$GW/server/music-online" "$GW/work/music-online"

# 网关配置目录必须显式指定: 默认会去找 <pkg>/data/.config.yaml(板上是空的),
# 结果整份默认配置生效(ASR=FunASR / LLM=ChatGLM / TTS=EdgeTTS)。
export XR_DATA_DIR="$GW/data"
export XR_CONFIG_FILE="$GW/data/.config.yaml"

# 端口探测: 优先用部署器推来的独立脚本(adb shell 的多层引号会损坏 python -c,
# /dev/tcp 在 setsid 新会话中也不可靠)。旧板路径(deploy-cloud-gateway /
# rollback-cloud-gateway)不推这个文件, 此时退回板内内联 socket 探测 ——
# 内联代码在板上直接执行, 没有 adb 引号问题。
PROBE=/data/xiaorui-gateway/probe-port.py
probe() {
    if [ -f "$PROBE" ]; then
        "$PY" "$PROBE" "$1" >/dev/null 2>&1
    else
        "$PY" -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$1" >/dev/null 2>&1
    fi
}

# --- 1) RKLLM 大模型 (NPU) ---
if ! probe 8112; then
    log "拉起 RKLLM 大模型 (加载 20-40 s)"
    setsid "$PY" "$SRV/llm_rkllm.py" --model "$MODEL" \
        >>"$GW/data/rkllm.log" 2>&1 </dev/null &
    LLM_PID=$!
    # oom_score_adj: 内存吃紧时内核 OOM killer 先杀可牺牲的, 别杀主链路。
    # -500 = 保护(同罗总开发笔记那套 RK3562 2GB 实测取值)。
    echo -500 > "/proc/$LLM_PID/oom_score_adj" 2>/dev/null || true
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
    GW_PID=$!
    # 网关是主链路, 内存吃紧时别让内核先杀它
    echo -500 > "/proc/$GW_PID/oom_score_adj" 2>/dev/null || true
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
