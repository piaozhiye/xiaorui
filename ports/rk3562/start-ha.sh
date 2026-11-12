#!/bin/sh
# RK3562 板上 Home Assistant 启动器(幂等): zram swap -> HA -> OOM 优先级。
#
# 由 /etc/init.d/S98xiaorui 开机调用, 也由 crond 每 2 分钟看护调用, 手动可跑。
# HA 只作 xiaorui 语音的设备控制面(灯/插座), 配置见 /data/ha-config/configuration.yaml。
#
# 内存背景(2026-11-11 实测, 2GB 板):
#   RKLLM 690M + 网关 760M(峰值) + HA 260M + 前端 66M ~= 1.75G, 无 swap 时
#   OOM 轮流杀进程(网关/HA 各被杀过)。zram 512M(lzo) + OOM 优先级后共存稳定:
#   HA 冷页换出, available 稳在 240M 左右。
HA_PY=/data/ha-env/bin/python
HA_CFG=/data/ha-config
LOG=$HA_CFG/ha.log

# 1) zram swap: 不占磁盘(压缩内存盘), HA/冷页换出代价小。
#    ★ 算法必须 zstd, 容量 768M: 2026-11-11 实测 lzo+512M 时压缩比 ≈1.0
#    (mm_stat: orig 331M -> compr 336M, 对 python/模型页完全无效, 白占内存
#    且 swap 很快打满); zstd 换出后同样页压缩比 2~3x。
#    已挂但配置不对(非 768M)时自动重设: swapoff(换回内存, avail 剧跌几秒)
#    -> reset -> 重设 -> swapon。
ZRAM_SIZE=805306368
sw_mem() { free -m 2>/dev/null | awk '/Swap/{print $2}'; }
zram_cfg_ok() {
    [ "$(sw_mem)" = "767" ] && grep -q zram0 /proc/swaps 2>/dev/null
}
if ! zram_cfg_ok; then
    grep -q zram0 /proc/swaps 2>/dev/null && swapoff /dev/zram0 2>/dev/null
    echo 1 > /sys/block/zram0/reset 2>/dev/null
    echo zstd > /sys/block/zram0/comp_algorithm 2>/dev/null
    echo $ZRAM_SIZE > /sys/block/zram0/disksize 2>/dev/null
    mkswap /dev/zram0 >/dev/null 2>&1
    swapon /dev/zram0 2>/dev/null
fi
echo 80 > /proc/sys/vm/swappiness 2>/dev/null

# 2) HA: 不在且无进程才拉(启动 ~40s; 就绪由 crond 下一轮确认, 不阻塞语音链路)
if ! (echo > /dev/tcp/127.0.0.1/8123) 2>/dev/null \
   && ! pgrep -f 'homeassista[n]t -c' >/dev/null 2>&1; then
    (setsid env LD_LIBRARY_PATH=/data/miniforge/lib "$HA_PY" -m homeassistant \
        -c "$HA_CFG" --skip-pip >>"$LOG" 2>&1 </dev/null &)
fi

# 3) OOM 优先级: 语音主链路(网关/RKLLM)保护, HA 优先牺牲。
#    进程重启后 pid 变, 所以每次调用都重设。
for p in $(pgrep -f 'xiaorui-gateway/server/[a]pp.py' 2>/dev/null); do
    echo -500 > /proc/$p/oom_score_adj 2>/dev/null; done
for p in $(pgrep -f 'llm_rkll[m]' 2>/dev/null); do
    echo -500 > /proc/$p/oom_score_adj 2>/dev/null; done
for p in $(pgrep -f 'homeassista[n]t -c' 2>/dev/null); do
    echo 200 > /proc/$p/oom_score_adj 2>/dev/null; done
exit 0
