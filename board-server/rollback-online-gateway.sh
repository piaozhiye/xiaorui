#!/bin/bash
# 板端联网网关回滚 (PC 侧, adb). 恢复到部署前状态, 不误伤 8000/RKLLM/生产前端.
# 用法: rollback-online-gateway.sh [-s serial] [--full]
#   默认: 停 8001 网关 + 恢复 server 树(最近 bak) + 恢复 start-all.sh + 摘除 crond 看护
#   --full: 额外恢复前端链(若验收轮改过 xiaorui.env/生产二进制, 从对应 .bak 恢复
#           并按 start-all 语义重启前端). 每步有明确校验输出, 末尾核验 8000/8112/前端.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
FULL=0
while [ $# -gt 0 ]; do
    case "$1" in
        -s) SERIAL="$2"; shift 2 ;;
        --full) FULL=1; shift ;;
        *) echo "未知参数 $1" >&2; exit 2 ;;
    esac
done
ADB="adb ${SERIAL:+-s $SERIAL}"
log() { echo "[rollback] $*"; }

# ---------- 1) 精确停 8001 网关进程(绝不 pkill -f 宽匹配) ----------
PIDS=$($ADB shell "pgrep -f 'xiaozhi-gateway/server/app.py'" | tr -d '\r' | grep -v '^$' || true)
if [ -n "$PIDS" ]; then
    log "停网关进程: $(echo $PIDS | tr '\n' ' ')"
    for p in $PIDS; do $ADB shell "kill $p" 2>/dev/null || true; done
    sleep 3
    LEFT=$($ADB shell "pgrep -f 'xiaozhi-gateway/server/app.py'" | tr -d '\r' | grep -v '^$' || true)
    [ -z "$LEFT" ] || { log "TERM 未退, KILL: $LEFT"; for p in $LEFT; do $ADB shell "kill -9 $p" 2>/dev/null || true; done; sleep 1; }
else
    log "网关进程不在(可能已停)"
fi
$ADB shell "ss -tln | grep -q ':8001 ' && echo '警告: 8001 仍被占用'" 2>/dev/null || true

# ---------- 2) 恢复 server 树 ----------
BAK=$($ADB shell "ls -dt /data/xiaorui-gateway/server.bak-* 2>/dev/null | head -1" | tr -d '\r')
CUR_SHA=$($ADB shell "sha256sum /data/xiaorui-server/board-scripts/xiaorui_live 2>/dev/null" | tr -d '\r' | awk '{print $1}')
if [ -n "$BAK" ]; then
    STAMP=$(date +%Y%m%d-%H%M%S)
    $ADB shell "mv /data/xiaorui-gateway/server /data/xiaorui-gateway/server.failed-$STAMP 2>/dev/null; \
                mv '$BAK' /data/xiaorui-gateway/server"
    log "server 树已从 $(basename "$BAK") 恢复(当前失败版本保留为 server.failed-$STAMP)"
else
    if $ADB shell "[ -d /data/xiaorui-gateway ]"; then
        log "无备份可恢复, 移除网关目录(首次部署即失败的场景)"
        $ADB shell "rm -rf /data/xiaorui-gateway/server /data/xiaorui-gateway/stage.* /data/xiaorui-gateway/.keytmp"
    else
        log "/data/xiaorui-gateway 不存在, 跳过"
    fi
fi

# ---------- 3) 恢复 start-all.sh ----------
if $ADB shell "grep -q 'xiaozhi-gateway boot hook' /data/xiaorui-server/start-all.sh 2>/dev/null"; then
    if $ADB shell "[ -f /data/xiaorui-server/start-all.sh.bak-gw ]"; then
        $ADB shell "cp /data/xiaorui-server/start-all.sh.bak-gw /data/xiaorui-server/start-all.sh"
        log "start-all.sh 已从 .bak-gw 恢复"
    else
        log "警告: start-all.sh 带 hook 但无备份, 手动摘除 hook 块"
        $ADB shell "sed -i '/xiaozhi-gateway boot hook/,+3d' /data/xiaorui-server/start-all.sh"
    fi
else
    log "start-all.sh 无 hook, 跳过"
fi

# ---------- 4) 摘除 crond 看护 ----------
$ADB shell "(crontab -l 2>/dev/null | grep -v 'xiaozhi-gateway/start-gateway.sh') | crontab - 2>/dev/null" \
    && log "crond 看护已摘除" || log "crontab 无看护项"

# ---------- 5) --full: 前端链恢复 ----------
if [ "$FULL" = "1" ]; then
    # xiaorui.env: 恢复到验收轮改之前的备份(acceptance 改前会备份 .bak-accept)
    if $ADB shell "[ -f /data/xiaorui-server/xiaorui.env.bak-accept ]"; then
        $ADB shell "cp /data/xiaorui-server/xiaorui.env.bak-accept /data/xiaorui-server/xiaorui.env"
        log "xiaorui.env 已恢复"
    fi
    # 生产二进制: 从轮换备份恢复最新一份
    BINBAK=$($ADB shell "ls -t /data/xiaorui-server/board-scripts/xiaorui_live.bak-* 2>/dev/null | head -1" | tr -d '\r')
    if [ -n "$BINBAK" ]; then
        $ADB shell "cp '$BINBAK' /data/xiaorui-server/board-scripts/xiaorui_live"
        log "前端二进制已从 $(basename "$BINBAK") 恢复"
    fi
    # 重启前端(杀精确 PID, 由 start-all 守护逻辑拉回? start-all 不自循环; 直接重启整机最稳:
    # 这里只重启前端进程)
    XPID=$($ADB shell "pgrep -x xiaorui_live" | tr -d '\r' | grep -v '^$' || true)
    [ -n "$XPID" ] && { $ADB shell "kill $XPID"; sleep 2; }
    $ADB shell "cd /data/xiaorui-server && . ./xiaorui.env 2>/dev/null; \
                setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib \
                /data/xiaorui-server/board-scripts/xiaorui_live > /tmp/xr_live.log 2>&1 &"
    sleep 5
fi

# ---------- 6) 校验基线 ----------
sleep 2
CHK=$($ADB shell "ss -tln | grep -cE ':8000 |:8112 '" | tr -d '\r')
NEW_SHA=$($ADB shell "sha256sum /data/xiaorui-server/board-scripts/xiaorui_live 2>/dev/null" | tr -d '\r' | awk '{print $1}')
log "校验: 8000+8112 监听数=$CHK (期望≥2); 前端二进制 sha256=${NEW_SHA:0:16}…"
[ "$CHK" -ge 2 ] || { echo "[rollback] FAIL: 8000/8112 基线异常" >&2; exit 1; }
if [ "$FULL" = "1" ]; then
    FPID=$($ADB shell "pgrep -x xiaorui_live" | tr -d '\r' | grep -v '^$' || true)
    [ -n "$FPID" ] || { echo "[rollback] FAIL: 前端未运行" >&2; exit 1; }
    [ "$CUR_SHA" != "$NEW_SHA" ] && log "前端二进制已变化: ${CUR_SHA:0:16}→${NEW_SHA:0:16}"
fi
log "回滚完成"
