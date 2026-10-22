#!/bin/bash
# 门禁: 板端产物不得含 LSE 原子指令(ARMv8.1+)。RK3576 = A72/A53(ARMv8.0,
# 无 LSE), 执行 casal 即 SIGILL。2026-10-16 语音重启失败根因: 工具链默认
# 架构把 stdatomic 的 CAS 编成 casal, xr_request_reboot 首条实质指令就毙命,
# fork 从未发生 -> 机器永远不重启、前端无输出消失。
# 构建侧修复 = 两个 build 脚本的 -march=armv8-a(LL/SC 循环, v8.0 合法);
# 本门禁防的是 flag 回退/工具链升级让 LSE 悄悄回来。
# 依赖先跑 build-board-live / build-board-audio-probe 产出二进制。
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TOOLCHAIN_ROOT="${XR_TOOLCHAIN_ROOT:-$HOME/projects/toolchains/gcc-arm-buildroot-aarch}"
OD="$TOOLCHAIN_ROOT/bin/aarch64-buildroot-linux-gnu-objdump"
[ -x "$OD" ] || { echo "FATAL: 交叉 objdump 不存在: $OD"; exit 2; }

BASES="cas swp ldadd ldclr ldeor ldset ldsmax ldsmin ldumax ldumin stadd stset stclr steor"
PAT=""
for b in $BASES; do PAT="${PAT:+$PAT|}$b"; done

FAILED=0
for bin in "$REPO/board/xiaorui_live" "$REPO/board/backend_audio_probe"; do
    [ -f "$bin" ] || { echo "FATAL: 缺构建产物 $bin (先跑 build 门禁)"; exit 2; }
    hits=$("$OD" -d "$bin" | awk -v pat="^(${PAT})[abhl]{0,3}$" '$3 ~ pat {print $3}' | sort | uniq -c)
    if [ -n "$hits" ]; then
        echo "FAIL $bin 含 LSE 原子指令(RK3576 上即 SIGILL):"
        echo "$hits"
        FAILED=1
    else
        echo "PASS $bin: 无 LSE 原子指令"
    fi
done
# 按需构建的产物: 存在才扫(缺省不挡), 构建脚本已统一 -march=armv8-a
for bin in "$REPO/vendor/rkaudio-vqe/libasound_module_pcm_rkaudio_vqe.so" "$REPO/board/xr_board_smoke"; do
    [ -f "$bin" ] || continue
    hits=$("$OD" -d "$bin" | awk -v pat="^(${PAT})[abhl]{0,3}$" '$3 ~ pat {print $3}' | sort | uniq -c)
    if [ -n "$hits" ]; then
        echo "FAIL $bin 含 LSE 原子指令(RK3576 上即 SIGILL):"
        echo "$hits"
        FAILED=1
    else
        echo "PASS $bin: 无 LSE 原子指令"
    fi
done
exit "$FAILED"
