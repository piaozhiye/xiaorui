#!/bin/bash
# 板端联网网关部署 (PC 侧编排, adb).
# 用法: deploy-online-gateway.sh [-s serial] [--key-file PATH] [--key-from-pc-config]
#                                [--skip-smoke] [--allow-auth-fail]
# 流程: preflight → 打包(源码+板侧脚本) → 推板 → 板上安装器(轮换备份+生成私有
#       配置+boot hook+crond 看护+启动就绪) → 板上体检 → 协议冒烟(hello+ping).
# 任一关键步失败自动回滚(rollback-online-gateway.sh). 成功不触碰: 8000 服务/
# RKLLM/生产前端二进制/xiaorui.env.
# key 缺省从 PC 上游私有配置提取, 以 600 临时文件推板, 用后即删, 全程不回显.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
KEY_FILE=""
KEY_FROM_PC=1
SKIP_SMOKE=0
ALLOW_AUTH_FAIL=0

log()  { echo "[deploy] $*"; }
die()  { echo "[deploy] FAIL: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -s) SERIAL="$2"; shift 2 ;;
        --key-file) KEY_FILE="$2"; KEY_FROM_PC=0; shift 2 ;;
        --skip-smoke) SKIP_SMOKE=1; shift ;;
        --allow-auth-fail) ALLOW_AUTH_FAIL=1; shift ;;
        *) die "未知参数 $1" ;;
    esac
done
ADB="adb ${SERIAL:+-s $SERIAL}"
PC_CFG=$HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server/data/.config.yaml

# ---------- preflight ----------
log "preflight: 串号 $SERIAL"
$ADB get-state >/dev/null 2>&1 || die "板子不在线(adb)"
GIT_SHA=$(git -C "$REPO" rev-parse --short HEAD)
GIT_DIRTY=$(git -C "$REPO" status --porcelain | wc -l)
[ "$GIT_DIRTY" -gt 0 ] && log "警告: 工作树有 $GIT_DIRTY 个未提交改动, 部署内容以工作树为准"
# grep -c 无匹配时退 1, 会触发 pipefail 静默终止 —— 板上 || true 兜住
PORTS=$($ADB shell "ss -tln | grep -cE ':8001 |:8004 ' || true" | tr -d '\r')
[ "$PORTS" = "0" ] || die "板上 8001/8004 已被占用"
$ADB shell "[ -x /data/miniforge/bin/python ] && /data/miniforge/bin/python -c 'import dashscope' >/dev/null 2>&1" \
    || { log "板上缺 dashscope 包, pip 安装…"; \
         $ADB shell "/data/miniforge/bin/pip install -q dashscope >/dev/null 2>&1" \
         || die "dashscope 安装失败(板上 pypi 不通?)"; }
$ADB shell "[ -d /data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker ]" || die "板上 matcha 模型缺失"

# key 提取(不回显): --key-file 直接用; 否则从 PC 私有配置取 LLM key
TMPKEY=$(mktemp /tmp/gwkey.XXXXXX)
chmod 600 "$TMPKEY"
trap 'rm -f "$TMPKEY" "$TGZ" 2>/dev/null' EXIT
if [ "$KEY_FROM_PC" = "1" ]; then
    [ -f "$PC_CFG" ] || die "PC 私有配置不存在($PC_CFG), 用 --key-file 指定"
    python3 - "$PC_CFG" "$TMPKEY" <<'EOF'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
k = d["LLM"]["AliLLM"]["api_key"]
assert k and len(k) > 10, "PC 配置里取不到 AliLLM.api_key"
open(sys.argv[2], "w").write(k)
EOF
else
    cp "$KEY_FILE" "$TMPKEY"
fi

# ---------- 打包 ----------
TGZ=$(mktemp /tmp/gw-payload.XXXXXX.tgz)
STAGE=$(mktemp -d /tmp/gw-stage.XXXXXX)
trap 'rm -rf "$TMPKEY" "$TGZ" "$STAGE" 2>/dev/null' EXIT
mkdir -p "$STAGE/pkg" "$STAGE/board-scripts"
cp -a "$DIR/pkg/." "$STAGE/pkg/"
find "$STAGE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
for f in start-gateway.sh check-network.sh gateway_client.py gateway-online.example.yaml start-frontend-cand.sh; do
    cp "$DIR/$f" "$STAGE/board-scripts/"
done
cp "$DIR/install-gateway-onboard.sh" "$STAGE/board-scripts/"
tar -C "$STAGE" -czf "$TGZ" pkg board-scripts
SHA_TGZ=$(sha256sum "$TGZ" | cut -c1-64)
NFILES=$(tar -tzf "$TGZ" | grep -v '/$' | wc -l)
log "打包完成: $NFILES 个文件, sha256=${SHA_TGZ:0:16}…"

# ---------- 推板 ----------
$ADB push "$TGZ" /data/xiaorui-gateway-incoming.tgz >/dev/null || die "推送 payload 失败"
$ADB push "$TMPKEY" /data/xiaorui-gateway-key.tmp >/dev/null || die "推送 key 失败"
$ADB push "$DIR/install-gateway-onboard.sh" /data/xiaorui-gateway-install.sh >/dev/null || die "推送安装器失败"
$ADB shell "chmod 700 /data/xiaorui-gateway-install.sh; chmod 600 /data/xiaorui-gateway-key.tmp"

# ---------- 板上安装(失败即回滚) ----------
if ! $ADB shell "sh /data/xiaorui-gateway-install.sh /data/xiaorui-gateway-incoming.tgz /data/xiaorui-gateway-key.tmp"; then
    log "板上安装失败, 执行回滚…"
    "$DIR/rollback-online-gateway.sh" -s "$SERIAL" || true
    die "安装失败已回滚"
fi
$ADB shell "rm -f /data/xiaorui-gateway-incoming.tgz /data/xiaorui-gateway-key.tmp /data/xiaorui-gateway-install.sh"

# ---------- 体检: 鉴权失败视为部署失败(除非显式容忍) ----------
NETOUT=$($ADB shell "sh /data/xiaorui-gateway/check-network.sh" 2>&1) || true
echo "$NETOUT" | sed 's/^/[netcheck] /'
if echo "$NETOUT" | grep -q '^FAIL api-'; then
    if [ "$ALLOW_AUTH_FAIL" = "0" ]; then
        log "云 API 鉴权失败, 回滚部署"
        "$DIR/rollback-online-gateway.sh" -s "$SERIAL" || true
        die "API 鉴权失败(详见上方 netcheck)"
    fi
    log "警告: API 鉴权失败但 --allow-auth-fail, 继续部署(如实记录)"
fi

# ---------- 冒烟: 协议连通(hello+ping, 不产生云调用) ----------
# 直接在板上跑客户端(板上 python 有 websockets): 不经 PC, 不建任何 ADB 隧道
if [ "$SKIP_SMOKE" = "0" ]; then
    if ! $ADB shell "/data/miniforge/bin/python /data/xiaorui-gateway/gateway_client.py --url ws://127.0.0.1:8001/xiaorui/v1/ 2>&1" | grep -q '"pong": *true'; then
        log "冒烟失败, 回滚部署"
        "$DIR/rollback-online-gateway.sh" -s "$SERIAL" || true
        die "冒烟失败(网关 8001 协议不通)"
    fi
    log "冒烟通过: hello+ping"
fi

log "部署完成: 8001 就绪, git=$GIT_SHA(dirty=$GIT_DIRTY), payload=${SHA_TGZ:0:16}…"
$ADB shell "grep -q payload_sha /data/xiaorui-gateway/DEPLOY-MANIFEST 2>/dev/null || \
    sed -i 's/^payload:.*/payload: payload.tgz payload_sha=$SHA_TGZ git=$GIT_SHA dirty=$GIT_DIRTY/' /data/xiaorui-gateway/DEPLOY-MANIFEST" || true
$ADB shell "cat /data/xiaorui-gateway/DEPLOY-MANIFEST" | sed 's/^/[manifest] /'
