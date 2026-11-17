#!/bin/bash
# RK3562 + ES8388 板端运行时检查: libc/ABI + RKLLM/NPU 冒烟。
#
# 分两部分:
#   1) ABI: 板端 glibc 版本 + Python 是否能跑(网关/ASR/TTS/LLM 全靠它)
#   2) NPU: librkllmrt.so 能否加载 + w8a8 模型能否在 NPU 上出字
#
# 任何一项 FAIL 都应该停下, 不要继续装整套服务。
#
# 用法: bash ports/rk3562-es8388/check-runtime.sh SERIAL
set -uo pipefail

SERIAL="${1:-}"
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
WORK=/tmp/opencode/rk3562-es8388
SRV="${XR_ES8388_SERVER_DIR:-/data/xiaorui-server}"
MODEL="${XR_ES8388_MODEL:-$SRV/qwen2.5-0.5b-instruct-rk3562-w8a8.rkllm}"
PY="${XR_ES8388_PYTHON:-/data/miniforge/bin/python3.14}"

if [ -z "$SERIAL" ]; then
    echo "用法: $0 SERIAL" >&2
    exit 2
fi
ADB="adb -s $SERIAL"
mkdir -p "$WORK"
RC=0
NPU_REMOTE=/tmp/xr-es8388-npu-check
PROBE=/data/xiaorui-gateway/probe-port.py

probe_port() {
    $ADB shell "$PY $PROBE $1" >/dev/null 2>&1
}

say() { printf '\n== %s ==\n' "$1"; }
ok()  { echo "  [OK]   $*"; }
bad() { echo "  [FAIL] $*"; RC=1; }

say "板子身份"
MODEL_NAME=$($ADB shell 'cat /proc/device-tree/model' 2>/dev/null | tr -d '\0')
case "$MODEL_NAME" in
    *RK3562*) ok "$MODEL_NAME" ;;
    *) bad "不是 RK3562: $MODEL_NAME"; exit 1 ;;
esac

# 端口探针必须以文件方式送板: 内联引号经 adb shell 会被吃掉, 造成假阴性。
# 放在板型确认之后: 不能往一台还没确认身份的板子上写东西。
if ! $ADB shell "test -f $PROBE" >/dev/null 2>&1; then
    $ADB shell "mkdir -p $(dirname "$PROBE")" >/dev/null 2>&1
    $ADB push "$REPO/board-server/probe-port.py" "$PROBE" >/dev/null 2>&1 \
        || bad "无法推送端口探针到 $PROBE"
fi

say "libc 版本(板上没有 ldd/getconf, 拉 libc.so.6 读版本串)"
$ADB pull /lib/libc.so.6 "$WORK/libc.so.6" >/dev/null 2>&1
GLIBC=$(strings "$WORK/libc.so.6" 2>/dev/null | grep -oE 'glibc 2\.[0-9]+' | head -1)
if [ -n "$GLIBC" ]; then ok "$GLIBC"; else bad "读不到 libc 版本"; fi

say "Python(板内网关/ASR/TTS/LLM 的运行时)"
PYVER=$($ADB shell "$PY -V" 2>/dev/null | tr -d '\0')
if echo "$PYVER" | grep -q Python; then ok "$PYVER"; else bad "Python 不可用: ${PYVER:-无输出}"; fi

say "RKLLM 运行时"
if $ADB shell "test -f $SRV/librkllmrt.so" 2>/dev/null; then
    ok "librkllmrt.so 已部署"
else
    bad "缺 $SRV/librkllmrt.so"
fi
# libgomp: librkllmrt.so 的 NEEDED 项, 板端镜像里没有, 必须自带
LOAD=$($ADB shell "LD_LIBRARY_PATH=$SRV/lib $PY -c \"
import ctypes
try:
    ctypes.CDLL('$SRV/librkllmrt.so')
    print('loaded')
except OSError as e:
    print('error:', e)
\"" 2>/dev/null | tr -d '\0' | tail -1)
case "$LOAD" in
    *loaded*) ok "librkllmrt.so 加载成功" ;;
    *) bad "librkllmrt.so 加载失败: ${LOAD:-无输出}(检查 $SRV/lib/libgomp.so.1)" ;;
esac

say "NPU 节点与频率"
NPU=$($ADB shell 'cat /sys/devices/platform/ff300000.npu/devfreq/ff300000.npu/cur_freq 2>/dev/null; ls /dev/dri/renderD* 2>/dev/null' 2>/dev/null | tr -d '\0' | tr '\n' ' ')
if echo "$NPU" | grep -q renderD; then ok "NPU 设备: $NPU"; else bad "没有 NPU render 节点: ${NPU:-无}"; fi

say "w8a8 模型 NPU 推理"
if ! $ADB shell "test -f $MODEL" 2>/dev/null; then
    bad "缺模型 $MODEL"
else
    # 起一个临时 RKLLM 服务(若已有服务在跑则复用)
    STARTED=0
    if ! probe_port 8112; then
        STARTED=1
        $ADB shell "cd $SRV && (setsid env LD_LIBRARY_PATH=$SRV/lib $PY llm_rkllm.py --model $MODEL >/tmp/rkllm-check.log 2>&1 </dev/null &)" >/dev/null 2>&1
        for _ in $(seq 1 40); do
            probe_port 8112 && break
            sleep 2
        done
    fi
    # 检查用的临时实例要收掉, 否则占着 8112 会让正式栈起不来
    # (用 [l] 括号技巧: 否则 pkill -f 会把执行这条命令的 adb shell 自己也杀掉)
    cleanup() {
        [ "$STARTED" = "1" ] && $ADB shell 'pkill -f "[l]lm_rkllm.py"' >/dev/null 2>&1
        return 0
    }
    trap cleanup EXIT
    if probe_port 8112; then
        # NPU 负载来自 debugfs: 没挂载的话采样会全是空, 报"没用到 NPU"是误导。
        if ! $ADB shell 'test -r /sys/kernel/debug/rknpu/load' >/dev/null 2>&1; then
            $ADB shell 'mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null' >/dev/null 2>&1 || true
        fi
        if $ADB shell 'test -r /sys/kernel/debug/rknpu/load' >/dev/null 2>&1; then
            echo "  (RKNPU load 节点可读)"
        else
            bad "读不到 /sys/kernel/debug/rknpu/load(debugfs 未挂载?), 无法取得 NPU 负载证据"
        fi
        # 在请求期间采样，而不是响应后才读取（推理完成时 NPU load 通常已回到 0%）。
        # 采样窗口给足(60s): 0.5B 模型在 0.8 TOPS 上数 30 个数可能超过 30s,
        # 窗口太短会在推理还没结束时就收工, 把"正在用 NPU"误判成"没用 NPU"。
        $ADB shell "rm -f $NPU_REMOTE.log $NPU_REMOTE.done $NPU_REMOTE.stop; (i=0; while [ \$i -lt 600 ] && [ ! -f $NPU_REMOTE.stop ]; do cat /sys/kernel/debug/rknpu/load >> $NPU_REMOTE.log 2>/dev/null; sleep 0.1; i=\$((i+1)); done; touch $NPU_REMOTE.done) >/dev/null 2>&1 </dev/null &"
        REPLY=$($ADB shell "LD_LIBRARY_PATH=$SRV/lib $PY - <<'PYEOF' 2>/dev/null
import json, urllib.request
body = json.dumps({'model':'local','messages':[{'role':'user','content':'请从1数到30，只输出数字，用空格分隔。'}]}).encode()
req = urllib.request.Request('http://127.0.0.1:8112/v1/chat/completions', data=body,
                             headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=180) as r:
    raw = r.read().decode(errors='replace')
out = []
for line in raw.splitlines():
    if line.startswith('data: '):
        try:
            d = json.loads(line[6:])
        except Exception:
            continue
        for ch in d.get('choices', []):
            c = (ch.get('delta') or {}).get('content')
            if c:
                out.append(c)
print(''.join(out)[:120])
PYEOF" 2>/dev/null | tr -d '\0' | tail -1)
        printf '%s\n' "$REPLY" > "$WORK/inference-response.txt"
        # 采样器在推理请求期间运行；推理结束后再多采 200ms，避免瞬时负载漏采。
        sleep 0.2
        $ADB shell "touch $NPU_REMOTE.stop" >/dev/null 2>&1
        for _ in $(seq 1 20); do
            $ADB shell "test -f $NPU_REMOTE.done" >/dev/null 2>&1 && break
            sleep 0.2
        done
        $ADB pull "$NPU_REMOTE.log" "$WORK/npu-load.log" >/dev/null 2>&1 || : > "$WORK/npu-load.log"
        $ADB shell "cat /tmp/rkllm-check.log /tmp/rkllm.log /data/xiaorui-gateway/data/rkllm.log 2>/dev/null" \
            2>/dev/null | tr -d '\0' > "$WORK/runtime.log"
        if [ "$STARTED" = "1" ]; then
            # 本次启动的服务: 日志一定是新的, 把旧内容清掉只留这次。
            : > "$WORK/runtime.log"
            $ADB shell "cat /tmp/rkllm-check.log 2>/dev/null" 2>/dev/null | tr -d '\0' \
                > "$WORK/runtime.log"
        else
            echo "  (复用已在跑的 RKLLM 服务; 运行横幅取自该服务的现有日志)"
        fi
        if python3 "$DIR/check-npu-evidence.py" \
            --runtime-log "$WORK/runtime.log" \
            --npu-load-log "$WORK/npu-load.log" \
            --response "$WORK/inference-response.txt"; then
            ok "RK3562 W8A8 实际 NPU 推理通过"
        else
            bad "NPU 使用/运行平台/模型精度未获实测证据(看 $WORK/runtime.log 与 npu-load.log)"
        fi
        $ADB shell "rm -f $NPU_REMOTE.log $NPU_REMOTE.done $NPU_REMOTE.stop" >/dev/null 2>&1
    else
        bad "RKLLM 服务没起来(看板上 /tmp/rkllm-check.log)"
    fi
fi

say "结论"
[ $RC -eq 0 ] && echo "  ABI + NPU/RKLLM 全部通过, 可以继续部署整套服务" \
               || echo "  有失败项, 先修再继续(不要装整套服务)"
exit $RC
