#!/bin/sh
# 一键拉起全离线语音助手: 本地大脑(RKLLM) + 板端网关
# 用法(板上): sh /data/xiaorui-server/start-all.sh
# 开机自启入口: /etc/init.d/S99xrvoice
#
# 2026-10-09: xz_stream 流式客户端不再自启 —— 服务端 ASR 队列积压未修,
# 且它会占住 USB 麦与 turn 模式冲突. 手动跑(修好后):
#   cd /data/xiaorui-server && setsid /data/miniforge/bin/python xz_stream.py > /tmp/xr_stream.log 2>&1 &
# 当前交互方式: PC 侧 run-board-turn.sh / 板上 /data/xiaorui-server/board-scripts/xz_board_turn.sh

# 音频 mixer 状态恢复(重启后全部复位, 必须每次设置)
# 2026-10-12: 改按卡名设置 —— USB 声卡/ZR 枚举顺序随启动竞态漂移(实测见过 card2/card4,
# DLPMerge 虚拟卡出现后 es8388 也从 card0 挪到 card1), 写死卡号会静默打到别的卡上
amixer -c Audio sset PCM 100% >/dev/null 2>&1
amixer -c Audio sset Mic 100% >/dev/null 2>&1
amixer -c rockchipes8388 sset Headphone on >/dev/null 2>&1
amixer -c rockchipes8388 sset Headphone 100% >/dev/null 2>&1
amixer -c rockchipes8388 sset Speaker on >/dev/null 2>&1
amixer -c rockchipes8388 sset Speaker 100% >/dev/null 2>&1
# ES8388 VQE 采音链(2026-10-20): SAI1 SDI1 Loopback 给 AEC 送立体声数字参考,
# 4ch=[活麦,空麦位,refL,refR]@48k; PGA 8(+24dB)。必须在起前端前设。
if ! sh /data/es8388-aec-mixer.sh; then
    echo "错误: ES8388 AEC mixer 初始化失败，停止启动" >&2
    exit 1
fi

# 定频保险: 开机 S45perf 已由 perf-lock 锁满 CPU/NPU/DDR, 这里幂等再补一次,
# 防止中途有人 perf-lock off 过. 手动解锁: perf-lock off
/usr/bin/perf-lock apply >/dev/null 2>&1 || true

# 2026-10-17: 旧 8000 本地栈(llm_rkllm+server/app.py)已退役 —— 8002 全本地网关取代,
# 目录删除前备份 /data/xiaorui-gateway/retired-8000-20261017.tar.gz。RKLLM 进程
# 由 8002 网关自行管理; 剩余 pkill 防的是历史残留进程占资源。
pkill -f "llm_rkllm.py" 2>/dev/null
pkill -f "xiaozhi-server/server/app.py" 2>/dev/null
pkill -f "xz_stream.py" 2>/dev/null

# 常驻语音前端 xiaorui_live (单进程 C: readi 采音+帧级VAD+内嵌流式ASR+WS+writei 播放)
# 可选前端环境(重启持久): /data/xiaorui-server/xiaorui.env 写
#   export XR_BACKEND=hybrid  / XR_CLOUD_URL=ws://网关:8001/xiaorui/v1/ 等(须 export)
[ -f /data/xiaorui-server/xiaorui.env ] && . /data/xiaorui-server/xiaorui.env
# 等 USB 声卡就绪(最多 20s): 枚举慢于 S99 时前端会在"开麦失败"上空转重试
# (2026-10-12 实例: 声卡不在, 单进程重试一整天 6509 次)
i=0
while [ $i -lt 20 ] && ! grep -q "\[Audio" /proc/asound/cards; do
    sleep 1; i=$((i+1))
done
grep -q "\[Audio" /proc/asound/cards || echo "警告: Audio 卡 20s 未出现, 前端将空转重试(物理拔插声卡可解)"
# 防双开: 已有实例则跳过
if ! pgrep -x xiaorui_live >/dev/null; then
    setsid env LD_LIBRARY_PATH=/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib \
        /data/xiaorui-server/board-scripts/xiaorui_live > /tmp/xr_live.log 2>&1 &
fi

echo "=== 状态 ==="
ss -tln | grep -E ":8001|:8002|:8003|:8005"
echo "=== 就绪: 对麦喊 '小瑞小瑞, xxx' 即应答; 日志 tail -f /tmp/xr_live.log ==="

# --- xiaozhi-gateway boot hook --- (deploy-online-gateway.sh 加入, 勿手删)
if [ -x /data/xiaorui-gateway/start-gateway.sh ]; then
    /data/xiaorui-gateway/start-gateway.sh || echo "警告: 联网网关未就绪, 离线链路不受影响"
fi
# --- Home Assistant boot hook --- (2026-10-17: 板上 HA, 8123; 米家设备控制中枢)
# pymicro-vad 无 aarch64 轮子且板上无 g++, assist_pipeline/default_config 报错属预期——
# 语音管线由 xiaorui 前端承担, HA 仅作设备控制面。首次引导(onboarding)需浏览器。
if ! ss -tln 2>/dev/null | grep -q ":8123 "; then
    mkdir -p /data/ha-config
    (setsid env LD_LIBRARY_PATH=/data/miniforge/lib /data/ha-env/bin/python -m homeassistant -c /data/ha-config \
        > /data/ha-config/ha.log 2>&1 < /dev/null &)
fi

# --- gateway-8002 boot hook --- (本地腿全本地栈: RKLLM+matcha+意图族)
if [ -x /data/xiaorui-gateway/start-gateway-8002.sh ]; then
    /data/xiaorui-gateway/start-gateway-8002.sh || echo "警告: 8002 离线网关未就绪"
fi


# homeassistant.local mDNS 应答器(小米 OAuth 回调域名硬编码, board 无 avahi)
if ! pgrep -f '[/]data/xiaorui-server/mdns-ha.py' >/dev/null; then
    (setsid env LD_LIBRARY_PATH=/data/miniforge/lib /data/ha-env/bin/python /data/xiaorui-server/mdns-ha.py > /tmp/mdns-ha.log 2>&1 < /dev/null &)
fi
