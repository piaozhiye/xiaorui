#!/bin/sh
# 本地大脑 RKLLM(8112) 归 8002 网关自管: 不在则拉起并等监听(模型加载秒级)。
# watch-gateways 每 5 分钟跑本脚本, 端口幂等, RKLLM 意外退出会被自愈。
# 2026-10-17: 旧 8000 栈退役曾误删其 RKLLM 拉起者, 8002 的 LLM 指向 8112
# 因此断供 —— 自此 8112 的生命周期归属本脚本。
PY8=/data/miniforge/bin/python
if ! ss -tln 2>/dev/null | grep -q "127.0.0.1:8112"; then
    pgrep -f "llm_rkllm[.]py" >/dev/null 2>&1 || \
        (setsid "$PY8" /data/xiaorui-server/llm_rkllm.py \
            --model /data/xiaorui-server/qwen25-1.5b-rk3576-w4a16.rkllm \
            > /tmp/xr_rkllm.log 2>&1 < /dev/null &)
    i=0
    while [ $i -lt 60 ] && ! ss -tln 2>/dev/null | grep -q "127.0.0.1:8112"; do
        sleep 1; i=$((i+1))
    done
    ss -tln 2>/dev/null | grep -q "127.0.0.1:8112" || \
        echo "警告: RKLLM(8112) 未就绪, 本地腿 LLM 问答将不可用(意图/ASR/TTS 不受影响)"
fi
if [ -f /data/xiaorui-gateway/instances/8002/runtime.env ]; then
    exec sh /data/xiaorui-gateway/start-gateway-instance.sh 8002
fi
# 板端联网网关启动器 (幂等, 就绪判定, 不依赖 PC/ADB)
# 位置: /data/xiaorui-gateway/start-gateway.sh
# 由 /data/xiaorui-server/start-all.sh 末尾守卫块调用, 也可手动执行.
# 退出码: 0=已就绪(或已启动且监听)  1=启动失败
GW_ROOT=/data/xiaorui-gateway
GW_SRV=$GW_ROOT/server-8002
PY=/data/miniforge/bin/python
LOG=/tmp/xz_gw_8002.log

# 前置: 目录/解释器/配置存在, 否则快速失败(不重试)
[ -f "$GW_SRV/app.py" ] || { echo "gateway: $GW_SRV/app.py 不存在"; exit 1; }
[ -x "$PY" ] || { echo "gateway: $PY 不存在"; exit 1; }
[ -f "$GW_SRV/data/.config.yaml" ] || { echo "gateway: 私有配置缺失"; exit 1; }

# 幂等1: 已监听 8002 就不再拉起
if ss -tln 2>/dev/null | grep -q "127.0.0.1:8002"; then
    exit 0
fi
# 幂等2: 进程在但还没监听(启动中)也不再拉起 —— 防双开
if pgrep -f "xiaorui-gateway/server-8002/app[.]py" >/dev/null 2>&1; then
    # 进程活着但 15s 内仍没监听视为启动失败, 提示看日志(不替它杀进程)
    i=0
    while [ $i -lt 15 ] && ! ss -tln 2>/dev/null | grep -q "127.0.0.1:8002"; do
        sleep 1; i=$((i+1))
    done
    ss -tln 2>/dev/null | grep -q "127.0.0.1:8002" && exit 0
    echo "gateway: 进程存在但 8002 迟迟未监听, 查 $LOG"
    exit 1
fi

cd "$GW_SRV" || exit 1
# subshell detach: bare setsid& gets reaped by parent shell lifecycle in boot env
(setsid "$PY" "$GW_SRV/app.py" > "$LOG" 2>&1 < /dev/null &)

# 就绪判定: 最多 45s 等 8002 监听(网关加载不含大模型, 正常 <10s;
# 云依赖是逐请求的, 这里不等网络 —— 网络晚到由每次请求自己恢复)
i=0
while [ $i -lt 45 ] && ! ss -tln 2>/dev/null | grep -q "127.0.0.1:8002"; do
    sleep 1; i=$((i+1))
done
if ss -tln 2>/dev/null | grep -q "127.0.0.1:8002"; then
    echo "gateway: ready (8002, ${i}s)"
    exit 0
fi
echo "gateway: 8002 未在 45s 内监听, 查 $LOG"
exit 1
