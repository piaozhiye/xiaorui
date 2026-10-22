#!/bin/sh
# PC 侧网关实例启动入口 —— 渲染配置 + 起进程, 不含网关源码。
#
# 代码永远是仓库里的 board-server/(同一份), 这里只是给出一组"跑在 PC 上"的
# 环境变量与配置覆盖。默认目录都在持久位置(~/.local/state), 不用 /tmp:
# 临时目录重启即失, 易被误当成"稳定实例"。
#
# 用法:
#   XR_TTS_MODEL_DIR=~/.local/share/xiaorui-gateway/matcha-icefall-zh-baker \
#   DASHSCOPE_API_KEY=... \
#   ./start.sh
#
# 常用覆盖项(都有默认值):
#   XR_CODE_DIR        网关源码目录(默认 <repo>/board-server/pkg)
#   XR_WORK_DIR        工作目录, app.py 的 cwd(默认 $XR_STATE_ROOT/work)
#   XR_DATA_DIR        数据/日志目录(默认 $XR_STATE_ROOT/data)
#   XR_CONFIG_FILE     渲染出的私有配置(默认 $XR_DATA_DIR/.config.yaml, 600)
#   XR_BASE_CONFIG     共用模板(默认 board-server/gateway-online.example.yaml)
#   XR_OVERLAY         平台覆盖层(默认本目录 config.overlay.example.yaml)
#   XR_PC_BEHAVIOR_CONFIG  板端 8000 的 config.yaml: 复用 prompt/wakeup_words 等行为段
#   XR_PC_PROMPT_FILE  只给 prompt 文本文件
#   XR_AUTH_KEY        前端 HMAC 用的签名密钥; 缺省则在 $XR_DATA_DIR/.auth_key
#                      生成一次并复用(600), 不要把它提交进仓库
set -eu

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO="$(CDPATH= cd -- "$DIR/../.." && pwd)"
: "${XR_STATE_ROOT:=$HOME/.local/state/xiaorui-gateway}"
: "${XR_CODE_DIR:=$REPO/board-server/pkg}"
: "${XR_WORK_DIR:=$XR_STATE_ROOT/work}"
: "${XR_DATA_DIR:=$XR_STATE_ROOT/data}"
: "${XR_CONFIG_FILE:=$XR_DATA_DIR/.config.yaml}"
: "${XR_BASE_CONFIG:=$REPO/board-server/gateway-online.example.yaml}"
: "${XR_OVERLAY:=$DIR/config.overlay.example.yaml}"
: "${XR_TTS_MODEL_DIR:=}"
: "${XR_PYTHON:=python3}"

[ -f "$XR_CODE_DIR/app.py" ] || { echo "[pc-gw] XR_CODE_DIR 下没有 app.py: $XR_CODE_DIR" >&2; exit 2; }
[ -f "$XR_BASE_CONFIG" ]     || { echo "[pc-gw] 找不到共用模板: $XR_BASE_CONFIG" >&2; exit 2; }
[ -f "$XR_OVERLAY" ]         || { echo "[pc-gw] 找不到覆盖层: $XR_OVERLAY" >&2; exit 2; }
# 网关依赖(dashscope/sherpa_onnx/aioconsole…)装在专用 venv 里, 系统 python3 通常缺
"$XR_PYTHON" -c 'import aioconsole, yaml' 2>/dev/null || {
    echo "[pc-gw] $XR_PYTHON 缺网关依赖; 指定装了依赖的解释器:" >&2
    echo "        XR_PYTHON=/path/to/gateway-venv/bin/python $0" >&2
    exit 2
}

mkdir -p "$XR_WORK_DIR" "$XR_DATA_DIR"
export XR_CODE_DIR XR_WORK_DIR XR_DATA_DIR XR_CONFIG_FILE
export XR_BASE_CONFIG XR_OVERLAY XR_TTS_MODEL_DIR XR_PYTHON

"$XR_PYTHON" - "$XR_BASE_CONFIG" "$XR_OVERLAY" "$XR_CONFIG_FILE" <<'PY'
import copy, hashlib, os, re, secrets, sys
import yaml

base_p, ov_p, out_p = sys.argv[1:4]


def load(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def merge(dst, src):
    """递归合并: 字典逐键合并, 其它类型(含列表)整体覆盖。

    覆盖层里值为 None 的键视为"未设置"跳过 —— YAML 里 `key:`(冒号后留空)就是
    None, 若按覆盖处理会把共用模板里的整段配置清空, 线上直接起不来。
    """
    for k, v in src.items():
        if v is None:
            print(f"[pc-gw] 覆盖层 {k}: 为空, 按未设置处理(保留共用模板值)")
            continue
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


cfg = merge(load(base_p), load(ov_p))

# 合并后自检: 覆盖层写错(比如把整段覆盖成空)时立刻停, 不要等起服务才报错。
for section in ("ASR", "LLM"):
    sel = (cfg.get("selected_module") or {}).get(section)
    if not ((cfg.get(section) or {}).get(sel) or {}).get("type"):
        sys.exit(f"[pc-gw] 合并后 {section}.{sel} 缺 type: 检查覆盖层是否把整段覆盖成空")

# 行为段: 板端实例的私有配置里带 prompt/wakeup_words(8000 栈已退役, 现在是
# /data/xiaorui-gateway/instances/8001/data/.config.yaml); PC 侧没有板端实例,
# 用 XR_PC_BEHAVIOR_CONFIG 直接复用同一个源, 避免人设/唤醒词表两处维护。
beh = os.environ.get("XR_PC_BEHAVIOR_CONFIG") or ""
if beh:
    src = load(beh)
    for key in ("prompt", "exit_commands", "wakeup_words", "voiceprint", "enable_greeting"):
        if key in src:
            cfg[key] = copy.deepcopy(src[key])
prompt_file = os.environ.get("XR_PC_PROMPT_FILE") or ""
if prompt_file:
    with open(prompt_file, encoding="utf-8") as f:
        cfg["prompt"] = f.read()

# 模型路径: 环境变量优先于覆盖层里的占位符
model_dir = os.environ.get("XR_TTS_MODEL_DIR") or ""
tts = cfg.setdefault("TTS", {}).setdefault("SherpaLocal", {})
if model_dir:
    tts["model_dir"] = os.path.expanduser(model_dir)
md = str(tts.get("model_dir") or "")
if not md or "REPLACE_WITH" in md:
    sys.exit("[pc-gw] 未设置 TTS 模型目录: 用 XR_TTS_MODEL_DIR=<网关主机上的 matcha 目录> "
             "或改覆盖层的 TTS.SherpaLocal.model_dir")
if not os.path.isdir(os.path.expanduser(md)):
    sys.exit(f"[pc-gw] TTS 模型目录不存在: {md}(模型在网关主机上, 不在板内)")

# 密钥: 占位替换。INJECT_KEY 是板端模板里的占位符, 这里从环境注入。
dash = os.environ.get("DASHSCOPE_API_KEY", "")
keys = {"LLM": os.environ.get("XR_LLM_KEY") or dash,
        "ASR": os.environ.get("XR_ASR_KEY") or dash}
left = []
for section, key in keys.items():
    for name, block in (cfg.get(section) or {}).items():
        if isinstance(block, dict) and block.get("api_key") in (None, "INJECT_KEY", ""):
            if key:
                block["api_key"] = key
            else:
                left.append(f"{section}.{name}.api_key")
if left:
    sys.exit("[pc-gw] 缺少密钥: " + ", ".join(left) + " —— 设 DASHSCOPE_API_KEY(或 XR_ASR_KEY/XR_LLM_KEY)")

# 前端鉴权密钥: 未显式给出时在数据目录生成一次并复用(600, 不入库)。
srv = cfg.setdefault("server", {})
ak = os.environ.get("XR_AUTH_KEY") or ""
keyfile = os.path.join(os.path.dirname(out_p), ".auth_key")
if not ak and os.path.exists(keyfile):
    with open(keyfile, encoding="utf-8") as f:
        ak = f.read().strip()
if not ak:
    ak = secrets.token_hex(32)
    fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(ak + "\n")
    print(f"[pc-gw] 已生成前端鉴权密钥 {keyfile}(600), 板端 XR_AUTH_KEY 需一致")
srv["auth_key"] = ak

tmp = out_p + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
os.chmod(tmp, 0o600)
os.replace(tmp, out_p)

raw = open(out_p, "rb").read()
print(f"[pc-gw] 配置渲染完成 {out_p} (600) sha256={hashlib.sha256(raw).hexdigest()[:16]}")
print(f"[pc-gw] 端口 ws={srv.get('port')} http={srv.get('http_port')} "
      f"auth={'on' if (srv.get('auth') or {}).get('enabled') else 'off'}")
print(f"[pc-gw] TTS={cfg.get('selected_module', {}).get('TTS')} model_dir={tts.get('model_dir')} sid={tts.get('sid')}")
ctx = ((cfg.get("ASR") or {}).get("Qwen3ASRFlash") or {}).get("context")
print(f"[pc-gw] ASR 偏置={'有' if ctx else '无'}(偏置只影响转写, 唤醒门独立)")
ww = cfg.get("wakeup_words")
print(f"[pc-gw] wakeup_words={'未设置(不继承行为段?)' if not ww else str(ww)[:40] + '…'}")
PY

if [ "${XR_RENDER_ONLY:-0}" = "1" ]; then
    echo "[pc-gw] XR_RENDER_ONLY=1: 只渲染配置, 不启动"
    exit 0
fi

# provider 探测(llm/asr/tts/intent)按 cwd 相对路径解析: board-server 的
# start-gateway-instance.sh 同样用 core 软链让共享代码在 work 目录里可见。
cd "$XR_WORK_DIR"
ln -sfn "$XR_CODE_DIR/core" "$XR_WORK_DIR/core"
# 共用默认配置 pkg/config.yaml 声明了 selected_module.VAD=SileroVAD(上游默认),
# 合并后一定存在, 所以 models/ 是硬依赖 —— 前端自己做 VAD, 但服务端仍会加载它。
if [ -n "${XR_MODELS_DIR:-}" ]; then
    ln -sfn "$XR_MODELS_DIR" "$XR_WORK_DIR/models"
fi
if [ ! -d "$XR_WORK_DIR/models/snakers4_silero-vad" ]; then
    echo "[pc-gw] 缺 models/snakers4_silero-vad(上游默认配置声明了 SileroVAD)" >&2
    echo "        指定模型目录: XR_MODELS_DIR=<含 snakers4_silero-vad 的目录>" >&2
    exit 2
fi
mkdir -p "$XR_WORK_DIR/tmp"
export PYTHONPATH="$XR_CODE_DIR${PYTHONPATH:+:$PYTHONPATH}"

LOG="$XR_DATA_DIR/gateway.log"
PIDFILE="$XR_DATA_DIR/gateway.pid"
HTTP_PORT=$("$XR_PYTHON" -c "import yaml;print((yaml.safe_load(open('$XR_CONFIG_FILE')) or {})['server']['http_port'])")

if command -v pgrep >/dev/null 2>&1 && pgrep -f "$XR_CODE_DIR/app.py" >/dev/null 2>&1; then
    echo "[pc-gw] 已有实例在跑($XR_CODE_DIR/app.py), 先停掉再启动" >&2
    exit 1
fi

echo "[pc-gw] 启动 $XR_PYTHON $XR_CODE_DIR/app.py (cwd=$XR_WORK_DIR) 日志=$LOG"
setsid "$XR_PYTHON" "$XR_CODE_DIR/app.py" >> "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

# 就绪探测: /health/ready 返回 ready=true 才算起来(与板端同一套判定)
i=0
while [ "$i" -lt 30 ]; do
    if "$XR_PYTHON" - "$HTTP_PORT" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health/ready", timeout=2) as r:
        sys.exit(0 if (json.load(r) or {}).get("ready") else 1)
except Exception:
    sys.exit(1)
PY
    then
        echo "[pc-gw] 就绪: http://127.0.0.1:$HTTP_PORT/health/ready (pid $(cat "$PIDFILE"))"
        exit 0
    fi
    i=$((i + 1))
    sleep 1
done
echo "[pc-gw] 30s 未就绪; 看 $LOG" >&2
exit 1
