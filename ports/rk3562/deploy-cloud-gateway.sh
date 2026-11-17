#!/bin/bash
# RK3562 板内网关: 板端(全离线) -> 云端(dashscope) 形态切换。
#
# 与 board-server/deploy-online-gateway.sh 的区别(为什么不用那个):
#   老脚本为"8000 离线小智形态"设计: 基线目录 /data/xiaorui-server/server、
#   boot hook 挂 start-all.sh、pgrep 匹配 xiaozhi- 老路径、start-gateway.sh
#   用 ss 判端口(板上 busybox ss 不可用)。当前 RK3562 是板内全离线形态
#   (S98xiaorui + start-board-stack + gwvenv), 结构对不上。
#   本脚本只做最小切换: 轮换 data/.config.yaml + 复用 start-board-stack.sh
#   重启网关(RKLLM 已在跑会被它幂等跳过)。生产代码/前端/启动链一概不动。
#
# 云端形态: ASR=qwen3-asr-flash, LLM=qwen-flash(openai 兼容), TTS=板端 Matcha,
#           VAD=板端 SileroVAD, 唤醒词表/人设与板端一致。
# 回滚: rollback-cloud-gateway.sh
#
# 用法: deploy-cloud-gateway.sh [-s serial] [--key-file PATH]
#   key 从 --key-file 或 XR_CLOUD_PC_CONFIG 的 LLM.AliLLM.api_key 提取，
#   600 权限临时文件推板, 用后即删, 不回显。
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
KEY_FILE=""
PC_CFG="${XR_CLOUD_PC_CONFIG:-}"
SVC=/data/xiaorui-gateway/data/.config.yaml   # 板上网关配置(被轮换的那个)
GW_VENV_PY=/data/gwvenv/bin/python

log() { echo "[cloud-deploy] $*"; }
die() { echo "[cloud-deploy] FAIL: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -s) SERIAL="$2"; shift 2 ;;
        --key-file) KEY_FILE="$2"; shift 2 ;;
        *) die "未知参数 $1" ;;
    esac
done
[ -n "$SERIAL" ] || SERIAL=$(adb devices | awk '/device$/{print $1}' | head -1)
[ -n "$SERIAL" ] || die "没有在线 adb 设备"
ADB="adb -s $SERIAL"

# ---------- 1) key 提取(不回显) ----------
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
TMPKEY="$TMP_DIR/key"
TMPYAML="$TMP_DIR/cloud.yaml"
touch "$TMPKEY" "$TMPYAML"
chmod 600 "$TMPKEY" "$TMPYAML"
if [ -n "$KEY_FILE" ]; then
    cp "$KEY_FILE" "$TMPKEY"
else
    [ -f "$PC_CFG" ] || die "PC 私有配置不存在($PC_CFG), 用 --key-file 指定"
    python3 - "$PC_CFG" "$TMPKEY" <<'EOF'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
k = d["LLM"]["AliLLM"]["api_key"]
assert k and len(k) > 10 and not k.startswith("INJECT"), "取不到有效的 AliLLM.api_key"
open(sys.argv[2], "w").write(k)
EOF
fi

# ---------- 2) 板上依赖: gwvenv 装 dashscope(qwen3_asr_flash 的 SDK) ----------
if ! $ADB shell "$GW_VENV_PY -c 'import dashscope'" >/dev/null 2>&1; then
    log "gwvenv 缺 dashscope, 安装(板上有网)…"
    $ADB shell "$GW_VENV_PY -m pip install -q dashscope" >/dev/null 2>&1 \
        || die "dashscope 安装失败(板上 pypi 不通?)"
fi
log "依赖 OK (dashscope/openai)"

# ---------- 3) 保存本次离线配置; 已在云端时校验并保留原快照 ----------
$ADB shell "$GW_VENV_PY - '$SVC' <<'PY'
import os
import shutil
import sys
import tempfile
import yaml

path = sys.argv[1]
backup = path + '.board-local-bak'
def modules(filename):
    with open(filename, encoding='utf-8') as config:
        return yaml.safe_load(config)['selected_module']
def offline(selected):
    return selected.get('ASR') == 'LocalSenseVoice' and selected.get('LLM') == 'LocalQwen'
selected = modules(path)
if offline(selected):
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.offline-')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            with open(path, encoding='utf-8') as source:
                shutil.copyfileobj(source, target)
        os.replace(temporary, backup)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
elif selected.get('ASR') == 'Qwen3ASRFlash' and selected.get('LLM') == 'AliLLM':
    if not offline(modules(backup)):
        raise SystemExit('离线配置快照无效，拒绝覆盖')
else:
    raise SystemExit('未知网关形态，拒绝切换')
PY" || die "保存或校验离线配置快照失败"

# ---------- 4) PC 侧合成云配置 ----------
# 底 = 板上现配置(保留 server 端口/唤醒词表/VAD/TTS 板内段), 只替换
# selected_module + LLM + ASR 三段为云模块, 并注入小瑞人设与 key。
# (TMPYAML 在开头已建, 失败路径由 trap 兜底清理)
$ADB shell "cat $SVC" | tr -d '\r' > "$TMPYAML.board" || die "拉取板内配置失败"
python3 - "$TMPYAML.board" "$TMPYAML" "$TMPKEY" <<'EOF'
import sys, yaml
board_cfg, out_cfg, keyfile = sys.argv[1:4]
key = open(keyfile).read().strip()
cfg = yaml.safe_load(open(board_cfg, encoding="utf-8"))
assert cfg, "板内配置解析失败"

cfg["selected_module"] = {"ASR": "Qwen3ASRFlash", "LLM": "AliLLM",
                          "TTS": "SherpaLocal", "Memory": "nomem",
                          "Intent": "function_call"}
cfg["LLM"] = {"AliLLM": {
    "type": "openai", "model_name": "qwen-flash",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": key, "temperature": 0.7, "max_tokens": 2048,
    "timeout": {"pool": 2, "connect": 3, "write": 5, "read": 30},
}}
cfg["ASR"] = {"Qwen3ASRFlash": {
    "type": "qwen3_asr_flash", "model_name": "qwen3-asr-flash",
    "api_key": key, "output_dir": "tmp/asr",
    "enable_lid": True, "enable_itn": True, "timeout": 20,
}}
# 人设: 与板内 RKLLM 的 persona 同源(小瑞), 大脑位置改成云端说法
cfg["prompt"] = ("你是小瑞，运行在 RK3562 开发板上的端侧语音助手，"
                 "大脑在云端大模型上。回答控制在两三句话内，适合语音播报，"
                 "不要用列表。")
yaml.dump(cfg, open(out_cfg, "w", encoding="utf-8"),
          allow_unicode=True, sort_keys=False)
print(f"  云配置合成 OK: 唤醒词 {len(cfg.get('wakeup_words', []))} 条, "
      f"TTS={cfg['TTS']['SherpaLocal']['model_dir'].split('/')[-1]}")
EOF
[ -s "$TMPYAML" ] || die "云配置合成失败"

# ---------- 5) 推板 + 重启网关 ----------
$ADB push "$TMPYAML" /data/xiaorui-gateway/data/.config.yaml.cloud-tmp >/dev/null
$ADB shell "chmod 600 /data/xiaorui-gateway/data/.config.yaml.cloud-tmp; \
    mv /data/xiaorui-gateway/data/.config.yaml.cloud-tmp $SVC"
log "云配置已就位, 重启网关(复用 start-board-stack, RKLLM 幂等跳过)…"
# 停网关进程: pkill -f 的模式不能原样出现在本条 adb shell 自己的命令行里,
# 否则 pkill 匹配到承载它的 shell 直接自杀, 后面的 start 永远不执行
# (2026-11-11 实测: rollback 因此留下半启动态)。用 [a]pp.py 正则技巧错开。
$ADB shell "pkill -f 'xiaorui-gateway/server/[a]pp.py' 2>/dev/null; sleep 2; true"
# 启动放板上后台(adb 秒回), 就绪在 PC 侧轮询 —— 云网关虽不加载大模型,
# 但 dashscope 首次 import + 板上 python 冷启动也要 10s+, adb 同步等有
# 被 PC 侧超时截断的风险(2026-11-11 实测, 留下半启动态)。
# 刷新板上的 start-board-stack.sh 与端口探针: 仓里的版本修了
# LD_LIBRARY_PATH 与探针路径, 板上是旧副本就享受不到。
adb -s "$SERIAL" push "$DIR/start-board-stack.sh" /data/xiaorui-gateway/start-board-stack.sh >/dev/null
adb -s "$SERIAL" push "$REPO/board-server/probe-port.py" /data/xiaorui-gateway/probe-port.py >/dev/null
adb -s "$SERIAL" shell 'chmod +x /data/xiaorui-gateway/start-board-stack.sh /data/xiaorui-gateway/probe-port.py'
$ADB shell "(setsid sh /data/xiaorui-gateway/start-board-stack.sh >>/data/xiaorui-gateway/data/stack.log 2>&1 </dev/null &); true"
i=0
while [ "$i" -lt 120 ]; do
    $ADB shell "(echo > /dev/tcp/127.0.0.1/8001) 2>/dev/null" && break
    sleep 3; i=$((i+3))
done
$ADB shell "(echo > /dev/tcp/127.0.0.1/8001) 2>/dev/null" || die "网关 8001 未就绪(120s), 查 gateway.log"

# ---------- 6) 形态确认 ----------
MOD=$($ADB shell "grep -A2 selected_module $SVC | grep ASR" | tr -d '\r ')
log "当前形态: $MOD"
echo "$MOD" | grep -q "Qwen3ASRFlash" || die "selected_module 没切到云 ASR"

# ---------- 7) 冒烟: 板上跑协议客户端(hello+ping, 不产生云调用) ----------
$ADB push "$REPO/board-server/gateway_client.py" /data/xiaorui-gateway/gateway_client.py >/dev/null
SMOKE=$($ADB shell "$GW_VENV_PY /data/xiaorui-gateway/gateway_client.py --url ws://127.0.0.1:8001/xiaorui/v1/ 2>&1" | tail -2)
echo "$SMOKE" | sed 's/^/[smoke] /'
echo "$SMOKE" | grep -q '"pong": *true' \
    || die "协议冒烟失败(hello+ping 不通)。板上已是云配置, 用 rollback-cloud-gateway.sh 恢复板端"

# ---------- 8) 云 API 鉴权实测(一次真实 LLM 调用, 有界) ----------
$ADB shell "$GW_VENV_PY - <<'PYEOF'
import os, urllib.request, json, yaml
cfg = yaml.safe_load(open('/data/xiaorui-gateway/data/.config.yaml'))
key = cfg['LLM']['AliLLM']['api_key']
req = urllib.request.Request(
    cfg['LLM']['AliLLM']['base_url'] + '/chat/completions',
    data=json.dumps({'model': 'qwen-flash',
                     'messages': [{'role': 'user', 'content': '回复:ok'}],
                     'max_tokens': 4}).encode(),
    headers={'Authorization': 'Bearer ' + key,
             'Content-Type': 'application/json'})
r = json.load(urllib.request.urlopen(req, timeout=15))
text = r['choices'][0]['message']['content']
print('[netcheck] LLM 鉴权 OK, 回复:', text[:20])
PYEOF" || die "云 LLM 鉴权失败(key/网络)。板上已是云配置, 用 rollback-cloud-gateway.sh 恢复板端"

rm -f "$TMPYAML" "$TMPYAML.board"
log "云端形态部署完成，请人工验收语音问答；回滚: bash ports/rk3562/rollback-cloud-gateway.sh"
