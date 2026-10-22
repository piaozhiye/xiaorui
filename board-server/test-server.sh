#!/bin/bash
# 板端网关协议测试入口: 用 gateway_client.py 对目标网关跑真实协议轮次.
# 用法: test-server.sh <ws-url> [--scenario text|audio|both] [--turns N]
#                         [--audio speech16k.wav] [--json-out r.json] [--real-api]
# --real-api 必须显式给出: 轮次会让网关真实调用云 LLM/ASR(计费), 无它只做
# hello+ping 连通冒烟. 轮数上限在客户端内封顶.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
# 优先用上游 PC venv(带 websockets/opuslib_next), 其次系统 python3
PY="${XR_TEST_PYTHON:-}"
if [ -z "$PY" ]; then
    for c in $HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python python3; do
        if "$c" -c 'import websockets, opuslib_next' >/dev/null 2>&1; then PY="$c"; break; fi
    done
fi
[ -n "$PY" ] || { echo "test-server: 找不到带 websockets+opuslib_next 的 python"; exit 2; }
exec "$PY" "$DIR/gateway_client.py" "$@"
