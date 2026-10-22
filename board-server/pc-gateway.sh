#!/bin/sh
# pc-gateway.sh -- PC 侧云端网关守护(与板端 hybrid 配套)
# 职责: ①网关服务端没起就 start ②板子在线而 adb reverse 隧道缺失就重建
# 服务端本体在独立单元 pc-xiaozhi.service(Restart=always) -- 停本守护不会杀服务端
# 用法(systemd 已配): systemctl --user {start|stop|status} pc-gateway
REPO="$HOME/rk/xiaozhi-edge-client"

while true; do
    # 1) 服务端健康: 按监听端口判断; 缺则 start 独立单元(幂等)
    if ! ss -tln 2>/dev/null | grep -q "127.0.0.1:8000"; then
        echo "$(date '+%m-%d %T') 服务端不在, start pc-xiaozhi"
        systemctl --user start pc-xiaozhi 2>/dev/null
        sleep 12
        ss -tln | grep -q "127.0.0.1:8000" \
            && echo "$(date '+%m-%d %T') 服务端已就绪" \
            || echo "$(date '+%m-%d %T') 警告: 服务端 12s 后仍未监听, 查 /tmp/pc-xiaozhi.log"
    fi
    # 2) 板子在线且隧道缺 -> 重建(adb reverse 不跨板子重启/USB 重插存活)
    if adb get-state >/dev/null 2>&1; then
        if ! adb reverse --list 2>/dev/null | grep -q "9100"; then
            adb reverse tcp:9100 tcp:8000 >/dev/null 2>&1 \
                && echo "$(date '+%m-%d %T') adb reverse tcp:9100 已重建"
        fi
    fi
    sleep 5
done
