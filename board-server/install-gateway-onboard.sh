#!/bin/sh
# 板上网关安装器 (BusyBox sh). 由 PC 侧 deploy-online-gateway.sh 调用, 也可板上手动:
#   install-gateway-onboard.sh <payload.tgz> <keyfile>
# 幂等: 重复执行会轮换旧 server 目录(保留 2 份 bak). 失败退出非零, 不留半装状态
# (已启动的进程由 PC 侧回滚脚本负责, 本脚本只管文件与启动).
set -u
PAYLOAD="${1:?用法: install-gateway-onboard.sh <payload.tgz> <keyfile>}"
KEYFILE="${2:?缺 keyfile}"
GW=/data/xiaorui-gateway
PY=/data/miniforge/bin/python
SRC8000=/data/xiaorui-server/server

fail() { echo "install: FAIL $*"; exit 1; }

[ -f "$PAYLOAD" ] || fail "payload 不存在"
[ -s "$KEYFILE" ] || fail "keyfile 为空"
[ -f "$SRC8000/app.py" ] || fail "8000 服务端目录不存在(基线缺失)"
[ -d /data/xiaorui-server/tts-matcha/matcha-icefall-zh-baker ] || fail "matcha 模型目录缺失"
mkdir -p "$GW" || fail "建目录失败"

# --- 轮换旧目录(保留 2 份 bak) ---
if [ -d "$GW/server" ]; then
    STAMP=$(date +%Y%m%d-%H%M%S)
    mv "$GW/server" "$GW/server.bak-$STAMP" || fail "轮换旧 server 失败"
    ls -dt "$GW"/server.bak-* 2>/dev/null | tail -n +3 | xargs -r rm -rf
fi
mkdir -p "$GW/server" || fail "建 server 失败"

# --- 解包: payload 含 pkg/(服务端源码) + board-scripts/(板侧脚本) ---
STAGE="$GW/stage.$$"
mkdir -p "$STAGE" || fail "建暂存目录失败"
tar -xzf "$PAYLOAD" -C "$STAGE" || { rm -rf "$STAGE" "$GW/server"; fail "解包失败"; }
[ -f "$STAGE/pkg/app.py" ] || { rm -rf "$STAGE" "$GW/server"; fail "解包后无 pkg/app.py"; }
# pkg/* → server/ (含隐藏文件如 .nativelib); board-scripts/ 一并带入供配置生成
cp -a "$STAGE/pkg/." "$GW/server/" || { rm -rf "$STAGE" "$GW/server"; fail "安装源码失败"; }
cp -a "$STAGE/board-scripts" "$GW/server/board-scripts" || { rm -rf "$STAGE" "$GW/server"; fail "安装板侧脚本目录失败"; }
rm -rf "$STAGE"

# --- 基础模板配置: 沿用 8000 的 config.yaml(占位 key 模板, 不含真实凭据) ---
cp "$SRC8000/config.yaml" "$GW/server/config.yaml" || fail "复制基础配置失败"
# 模板 selected_module 会选中 SileroVAD(merge 后仍生效), 模型沿用 8000 的目录
[ -e "$GW/server/models" ] || ln -s "$SRC8000/models" "$GW/server/models" || true

# --- 生成私有配置 data/.config.yaml: 模板 + 8000 的 prompt 人设 + key 注入 ---
# key 只在 python 进程内存与 600 权限文件间流转, 不回显
chmod 600 "$KEYFILE"
"$PY" - "$GW/server" "$KEYFILE" "$SRC8000/data/.config.yaml" "$GW/server/board-scripts/gateway-online.example.yaml" <<'EOF' || { rm -rf "$GW/server"; fail "私有配置生成失败"; }
import sys, os, yaml
gw_srv, keyfile, cfg8000, example = sys.argv[1:5]
key = open(keyfile).read().strip()
assert key and not key.startswith('INJECT'), 'keyfile 内容不是真实 key'
tpl = yaml.safe_load(open(example))
# 继承 8000 运行配置里的 prompt 人设(保持小瑞人格一致), 没有则继承模板配置
try:
    p = (yaml.safe_load(open(cfg8000)) or {}).get('prompt')
except Exception:
    p = None
if not p:
    try:
        p = (yaml.safe_load(open(os.path.dirname(cfg8000) + '/config.yaml')) or {}).get('prompt')
    except Exception:
        p = None
if p:
    tpl['prompt'] = p
s = yaml.dump(tpl, allow_unicode=True, sort_keys=False)
s = s.replace('INJECT_KEY', key)
os.makedirs(gw_srv + '/data', mode=0o700, exist_ok=True)
out = gw_srv + '/data/.config.yaml'
open(out, 'w').write(s)
os.chmod(out, 0o600)
print('config-written (no key echoed)')
EOF
rm -f "$KEYFILE"

# --- 安装板侧脚本 (payload 的 board-scripts/) ---
for f in start-gateway.sh check-network.sh gateway_client.py gateway-online.example.yaml start-frontend-cand.sh; do
    if [ -f "$GW/server/board-scripts/$f" ]; then
        cp "$GW/server/board-scripts/$f" "$GW/$f"
        chmod 755 "$GW/$f" 2>/dev/null || true
    fi
done
[ -x "$GW/start-gateway.sh" ] || fail "payload 缺 start-gateway.sh"
[ -x "$GW/check-network.sh" ] || fail "payload 缺 check-network.sh"

# --- 开机自启接入: start-all.sh 尾部守卫块(幂等: marker 已存在则跳过) ---
SALL=/data/xiaorui-server/start-all.sh
MARKER='# --- xiaozhi-gateway boot hook ---'
if grep -q "$MARKER" "$SALL" 2>/dev/null; then
    echo "install: boot hook 已存在, 跳过"
else
    cp "$SALL" "$SALL.bak-gw"          # 只备份一次(已存在则不覆盖)
    cat >> "$SALL" <<'HOOK'

# --- xiaozhi-gateway boot hook --- (deploy-online-gateway.sh 加入, 勿手删)
if [ -x /data/xiaorui-gateway/start-gateway.sh ]; then
    /data/xiaorui-gateway/start-gateway.sh || echo "警告: 联网网关未就绪, 离线链路不受影响"
fi
HOOK
    echo "install: boot hook 已接入 $SALL (备份 $SALL.bak-gw)"
fi

# --- crond 看护(可选): 进程崩溃 5 分钟内自动拉回; crond 不在则跳过 ---
if pgrep -x crond >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -v 'xiaozhi-gateway/start-gateway.sh'; \
     echo '*/5 * * * * /data/xiaorui-gateway/start-gateway.sh >/dev/null 2>&1') | crontab - 2>/dev/null \
        && echo "install: crond 看护已配置" || echo "install: crontab 配置失败(仅影响崩溃自愈)"
else
    echo "install: crond 未运行, 跳过看护(崩溃需手动 start-gateway.sh)"
fi

# --- 启动 + 就绪 ---
sh "$GW/start-gateway.sh" || fail "网关启动失败(查 /tmp/xz_gw.log)"

# --- manifest (无凭据字段) ---
{
    echo "installed_utc: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "payload: $PAYLOAD sha256=$(sha256sum "$PAYLOAD" | cut -c1-16)…"
    echo "server_files: $(find "$GW/server" -name '*.py' | wc -l) py"
    echo "config: ports 8001/8004; LLM=openai:qwen-flash; ASR=qwen3_asr_flash; TTS=sherpa_local(matcha)"
} > "$GW/DEPLOY-MANIFEST"
echo "install: OK (8001 就绪)"
exit 0
