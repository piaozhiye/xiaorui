#!/bin/bash
# RK3562 板上 Home Assistant 一键部署(PC 侧编排, adb)。
#
# 做什么:
#   1. /data/ha-env venv + pip install homeassistant(已装则跳过, 首次 ~10 分钟)
#   2. 推 configuration.yaml(最小设备控制面, 含一盏模板虚拟灯)/ha-onboard.py/start-ha.sh
#   3. 起 HA(zram swap + OOM 优先级由 start-ha.sh 管), 等 8123 就绪
#   4. 无头引导(ha-onboard.py, 幂等) -> 3650 天长期令牌 -> ha_token.txt
#      (网关 haIntent 运行时读该文件, 无需重启网关)
#   5. 推新版 init-S98xiaorui(HA 开机自启 + crond 每 2 分钟看护)
#
# 用法: deploy-ha.sh [-s serial]
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
HA_ADMIN_PWD="${XR_HA_ADMIN_PWD:-}"

log() { echo "[ha-deploy] $*"; }
die() { echo "[ha-deploy] FAIL: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -s) SERIAL="$2"; shift 2 ;;
        *) die "未知参数 $1" ;;
    esac
done
[ -n "$HA_ADMIN_PWD" ] || die "请通过 XR_HA_ADMIN_PWD 显式提供 HA 密码(已有账号须使用原密码)"
[ -n "$SERIAL" ] || SERIAL=$(adb devices | awk '/device$/{print $1}' | head -1)
[ -n "$SERIAL" ] || die "没有在线 adb 设备"
ADB="adb -s $SERIAL"

# ---------- 1) HA 安装(幂等) ----------
$ADB push "$DIR/requirements-ha.lock" /data/requirements-ha.lock >/dev/null
if $ADB shell '[ -x /data/ha-env/bin/python ]' >/dev/null 2>&1 \
   && $ADB shell '/data/ha-env/bin/python -c "import homeassistant"' >/dev/null 2>&1; then
    log "HA 已安装, 跳过"
else
    log "安装 homeassistant(首次约 10 分钟, 后台下载编译依赖)…"
    $ADB shell '/data/miniforge/bin/python -m venv /data/ha-env && \
        /data/ha-env/bin/pip -q install --no-deps -i https://pypi.tuna.tsinghua.edu.cn/simple \
            -r /data/requirements-ha.lock' \
        || die "homeassistant 安装失败"
fi

# ---------- 2) 推文件 ----------
$ADB shell 'mkdir -p /data/ha-config'
$ADB push "$DIR/ha-onboard.py" /data/xiaorui-server/ha-onboard.py >/dev/null
$ADB push "$DIR/start-ha.sh"   /data/xiaorui-server/start-ha.sh >/dev/null
$ADB shell 'chmod +x /data/xiaorui-server/start-ha.sh'
# configuration.yaml 只在板上不存在时推: 已有配置不动(保住用户在 HA 里
# 加的实体/集成; 要强刷配置手动 adb push)。
if ! $ADB shell '[ -s /data/ha-config/configuration.yaml ]' >/dev/null 2>&1; then
    $ADB push "$DIR/ha-configuration.yaml" /data/ha-config/configuration.yaml >/dev/null
    log "configuration.yaml 已推(首次)"
else
    log "configuration.yaml 已存在, 不覆盖"
fi

# ---------- 3) 起 HA ----------
$ADB shell 'sh /data/xiaorui-server/start-ha.sh'
log "等 HA 8123 就绪(首次 ~60s)…"
i=0
while [ "$i" -lt 150 ]; do
    $ADB shell '(echo > /dev/tcp/127.0.0.1/8123) 2>/dev/null' && break
    sleep 4; i=$((i+4))
done
$ADB shell '(echo > /dev/tcp/127.0.0.1/8123) 2>/dev/null' || die "HA 未就绪(150s), 查 /data/ha-config/ha.log"

# ---------- 4) 无头引导 + 长期令牌 ----------
log "无头引导(onboarding + 3650 天长期令牌)…"
printf '%s' "$HA_ADMIN_PWD" | $ADB shell -T \
    'LD_LIBRARY_PATH=/data/miniforge/lib /data/ha-env/bin/python /data/xiaorui-server/ha-onboard.py --password-stdin' \
    2>&1 | sed 's/^/[onboard] /'
$ADB shell '[ -s /data/xiaorui-server/ha_token.txt ]' >/dev/null 2>&1 \
    || die "ha_token.txt 没生成"

# ---------- 5) 开机自启 + 看护 ----------
$ADB push "$DIR/init-S98xiaorui" /etc/init.d/S98xiaorui >/dev/null
$ADB shell 'chmod +x /etc/init.d/S98xiaorui' || die "更新开机脚本权限失败"
$ADB shell '
    (crontab -l 2>/dev/null | grep -v "xiaorui-server/start-ha.sh";
     echo "*/2 * * * * /data/xiaorui-server/start-ha.sh >/dev/null 2>&1") | crontab -' \
    || die "更新 HA 看护任务失败"
$ADB shell 'pgrep -x crond >/dev/null 2>&1 ||
    (setsid /usr/sbin/crond >/dev/null 2>&1 </dev/null &)'
log "S98 已更新(HA 自启 + crond 看护)"

log "部署完成。语音验证: PC 放 tests/audio/wake2_ha_on2.wav, 灯应答'好的，灯开了'"
log "  灯态: adb shell 'curl -s -H \"Authorization: Bearer \$(cat /data/xiaorui-server/ha_token.txt)\" http://127.0.0.1:8123/api/states/light.test_light'"
