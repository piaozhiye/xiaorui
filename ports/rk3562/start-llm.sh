#!/bin/bash
# 启动本机 Qwen (llama.cpp) OpenAI 兼容服务, 供 xiaorui 网关当 LLM 用。
#
# 用法:
#   ports/rk3562/start-llm.sh            # 前台
#   ports/rk3562/start-llm.sh --daemon   # 后台常驻
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
VENV="$REPO/.venv-qwen"
MODEL="${XR_LLM_MODEL:-$REPO/models/qwen2.5-1.5b-instruct-q4_k_m.gguf}"
PORT="${XR_LLM_PORT:-8113}"
THREADS="${XR_LLM_THREADS:-8}"
LOG="${XR_LLM_LOG:-$HOME/.local/state/xiaorui-gateway/llm.log}"

[ -f "$MODEL" ] || { echo "缺模型: $MODEL" >&2; exit 2; }
[ -x "$VENV/bin/python" ] || { echo "缺 venv: $VENV" >&2; exit 2; }
mkdir -p "$(dirname "$LOG")"

CMD=("$VENV/bin/python" -m llama_cpp.server
     --model "$MODEL"
     --host 127.0.0.1
     --port "$PORT"
     --n_threads "$THREADS"
     --chat_format chatml)

if [ "${1:-}" = "--daemon" ]; then
    setsid "${CMD[@]}" >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$(dirname "$LOG")/llm.pid"
    echo "[llm] 后台启动 pid=$! log=$LOG"
    for i in $(seq 1 60); do
        if curl -s --max-time 2 "http://127.0.0.1:$PORT/v1/models" | grep -q '"id"'; then
            echo "[llm] 就绪: http://127.0.0.1:$PORT/v1"
            exit 0
        fi
        sleep 2
    done
    echo "[llm] 120s 未就绪, 看 $LOG" >&2
    exit 1
fi

exec "${CMD[@]}"
