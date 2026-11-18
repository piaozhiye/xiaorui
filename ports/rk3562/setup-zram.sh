#!/bin/sh
# RK3562 2GB 板: zram 压缩 swap(内存兜底)。
#
# 为什么必须有(2026-11-18 实测):
#   板子出厂 SwapTotal=0。板内常驻的 NPU 模型内存**不可回收**:
#     RockASR helper  558MB (DRM GEM shmem, 映射在 /dev/dri/card1)
#     RockTTS helper  229MB (同上)
#     RKLLM           604MB (Private_Dirty, 不可回收)
#     网关+前端+UI+系统 ~280MB
#     合计 ~1670MB / 1963MB, 余量只有 290MB —— 内核 OOM 会干掉 init,
#     表现为"整机黑屏死机"(2026-11-18 实测三次)。
#
# 算法必须 zstd(罗总开发笔记踩过的坑):
#   默认 lzo 在这些进程页上压缩比≈1.0(mm_stat: orig 331M -> compr 336M,
#   完全白换还把 swap 撑满); 同样页 zstd 能压到 1/4.4。
#   ★ 内核 CONFIG_ZRAM_DEF_COMP_ZSTD 没开(默认 lzo-rle), 但 zstd 后端编译进去了:
#     运行时 /sys/block/zram0/comp_algorithm 里能看到 "zstd", 直接写即可。
#
# 每次开机都跑一遍并校验: 被别的脚本改过就重设(笔记里就是这么做的)。
# 本脚本独立于 Home Assistant —— HA 停了(2GB 板要给 NPU 模型让路),
# zram 兜底必须还在。
set -u

ZRAM_DEV=${XR_ZRAM_DEV:-/dev/zram0}
ZRAM_SYS=${XR_ZRAM_SYS:-/sys/block/zram0}
SIZE_MB=${XR_ZRAM_SIZE_MB:-768}
ALGO=${XR_ZRAM_ALGO:-zstd}
SWAPPINESS=${XR_ZRAM_SWAPPINESS:-80}

log() { echo "[zram] $*"; }

[ -e "$ZRAM_SYS" ] || { log "板子没有 zram, 跳过"; exit 0; }

want_bytes=$((SIZE_MB * 1024 * 1024))

cur_algo=$(cat "$ZRAM_SYS/comp_algorithm" 2>/dev/null || echo "")
cur_size=$(cat "$ZRAM_SYS/disksize" 2>/dev/null || echo 0)
swapped=0
grep -q "$ZRAM_DEV" /proc/swaps 2>/dev/null && swapped=1

# 配置不对就整体重设。改算法要求设备为空, 所以先 swapoff + reset。
if [ "$swapped" = "1" ] && [ "$cur_size" = "$want_bytes" ] \
   && echo "$cur_algo" | grep -q "\[$ALGO\]"; then
    :
else
    log "重设 zram (算法=$cur_algo 大小=$((cur_size / 1024 / 1024))MB 已挂=$swapped)"
    [ "$swapped" = "1" ] && swapoff "$ZRAM_DEV" 2>/dev/null
    # reset 才能在非空状态下换算法; 老内核没这个节点就直接改(会在非空时失败)
    echo 1 > "$ZRAM_SYS/reset" 2>/dev/null || true
    if ! echo "$ALGO" > "$ZRAM_SYS/comp_algorithm" 2>/dev/null; then
        log "切 $ALGO 失败, 退回 lzo-rle"
        echo lzo-rle > "$ZRAM_SYS/comp_algorithm" 2>/dev/null || true
    fi
    echo "$want_bytes" > "$ZRAM_SYS/disksize" 2>/dev/null \
        || { log "设置 disksize 失败"; exit 1; }
    mkswap "$ZRAM_DEV" >/dev/null 2>&1 || true
    swapon "$ZRAM_DEV" 2>/dev/null || { log "swapon 失败"; exit 1; }
    log "已按 ${SIZE_MB}MB/$ALGO 重设"
fi

# swappiness: 80 —— 宁可把冷页压缩换出(在 zram 里, 不碰磁盘), 也不要 OOM。
cur_swap=$(cat /proc/sys/vm/swappiness 2>/dev/null || echo 0)
if [ "$cur_swap" != "$SWAPPINESS" ]; then
    echo "$SWAPPINESS" > /proc/sys/vm/swappiness 2>/dev/null \
        && log "swappiness $cur_swap -> $SWAPPINESS"
fi

log "状态: $(grep "$ZRAM_DEV" /proc/swaps 2>/dev/null | awk '{print "size="$3"KB used="$4"KB"}') 算法=$(cat "$ZRAM_SYS/comp_algorithm" 2>/dev/null)"
exit 0
