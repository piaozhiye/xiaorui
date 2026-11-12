#!/bin/bash
# RK3562 板内网关: 云端 -> 板端(全离线) 形态回滚。
# 恢复 deploy-cloud-gateway.sh 备份的 .config.yaml.board-local-bak 并重启网关。
# 用法: rollback-cloud-gateway.sh [-s serial]
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
SVC=/data/xiaorui-gateway/data/.config.yaml

log() { echo "[cloud-rollback] $*"; }
die() { echo "[cloud-rollback] FAIL: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -s) SERIAL="$2"; shift 2 ;;
        *) die "未知参数 $1" ;;
    esac
done
[ -n "$SERIAL" ] || SERIAL=$(adb devices | awk '/device$/{print $1}' | head -1)
[ -n "$SERIAL" ] || die "没有在线 adb 设备"
ADB="adb -s $SERIAL"

$ADB shell "[ -f ${SVC}.board-local-bak ]" >/dev/null 2>&1 \
    || die "板内配置备份不存在(${SVC}.board-local-bak), 无法回滚"

$ADB shell "/data/gwvenv/bin/python - '$SVC' <<'PY'
import os
import shutil
import sys
import tempfile
import yaml

path = sys.argv[1]
backup = path + '.board-local-bak'
with open(backup, encoding='utf-8') as source:
    selected = yaml.safe_load(source)['selected_module']
if selected.get('ASR') != 'LocalSenseVoice' or selected.get('LLM') != 'LocalQwen':
    raise SystemExit('快照不是板端离线配置，拒绝恢复')
descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.restore-')
try:
    with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
        with open(backup, encoding='utf-8') as source:
            shutil.copyfileobj(source, target)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY" || die "恢复配置失败"
log "板内配置已恢复, 重启网关…"
# 停网关进程: pkill -f 的模式不能原样出现在本条 adb shell 自己的命令行里,
# 否则 pkill 匹配到承载它的 shell 直接自杀, 后面的 start 永远不执行
# (2026-11-11 实测: 本脚本首版就栽在这)。用 [a]pp.py 正则技巧错开。
$ADB shell "pkill -f 'xiaorui-gateway/server/[a]pp.py' 2>/dev/null; sleep 2; true"
# 启动放板上后台(adb 秒回), 就绪在 PC 侧轮询 —— 板内网关加载 ASR/TTS 模型
# 要 20s+, adb 同步等会被 PC 侧超时截断(2026-11-11 实测), 留下半启动态。
$ADB shell "(setsid sh /data/xiaorui-gateway/start-board-stack.sh >>/data/xiaorui-gateway/data/stack.log 2>&1 </dev/null &); true"
i=0
while [ "$i" -lt 120 ]; do
    $ADB shell "(echo > /dev/tcp/127.0.0.1/8001) 2>/dev/null" && break
    sleep 3; i=$((i+3))
done
$ADB shell "(echo > /dev/tcp/127.0.0.1/8001) 2>/dev/null" || die "网关 8001 未就绪(120s)"

MOD=$($ADB shell "grep -A3 selected_module $SVC" | tr -d '\r')
echo "$MOD" | grep -q "LocalSenseVoice" || die "selected_module 没回到 Local"
log "已回到板端形态: ASR=LocalSenseVoice LLM=LocalQwen"
