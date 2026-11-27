#!/bin/bash
set -euo pipefail

directory="$(cd "$(dirname "$0")" && pwd)"
# 默认用仓内 vendored 的 LVGL 8.4（头文件 + 预编译库，MIT）；需要换 SDK 时设
# XR_LVGL_ROOT 覆盖即可。
lvgl_root="${XR_LVGL_ROOT:-$directory/third_party/lvgl}"
mode="${1:-aarch64}"
case "$mode" in
    aarch64)
        toolchain="${XR_TOOLCHAIN_ROOT:-$HOME/projects/toolchains/gcc-arm-buildroot-aarch}"
        compiler="$toolchain/bin/aarch64-buildroot-linux-gnu-gcc"
        sysroot="$toolchain/aarch64-buildroot-linux-gnu/sysroot"
        lvgl_library="${XR_LVGL_LIBRARY:-$lvgl_root/lib/aarch64/liblvgl.so}"
        wayland_library="${XR_WAYLAND_LIBRARY:?Set XR_WAYLAND_LIBRARY to target libwayland-client.so.0}"
        # ★ XR_WAYLAND_LIBRARY 必须指向 **wayland >= 1.20** 的库: 1.20 引入
        #   wl_proxy_marshal_flags, 旧版链接会报 undefined reference。
        #   toolchain 的 sysroot 自带的是旧版, 直接用它构建会失败; 从板上拉
        #   一份最省事(板上的版本必然能跑 UI):
        #     adb pull /usr/lib/libwayland-client.so.0 /tmp/wayland/
        #     XR_WAYLAND_LIBRARY=/tmp/wayland/libwayland-client.so.0 bash ui/build.sh aarch64
        #   2026-11-20 实测: 用 sysroot 的旧库 -> undefined reference to
        #   'wl_proxy_marshal_flags'; 用板上拉下来的库 -> 构建通过。
        flags=(-march=armv8-a -Wl,--allow-shlib-undefined)
        export LD_LIBRARY_PATH="$toolchain/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        ;;
    native)
        compiler="${CC:-gcc}"
        lvgl_library="${XR_LVGL_LIBRARY:-$lvgl_root/lib/x86/liblvgl.so}"
        wayland_library="${XR_HOST_WAYLAND_LIBRARY:--lwayland-client}"
        flags=()
        ;;
    *) echo "usage: $0 [aarch64|native]" >&2; exit 2 ;;
esac

find_xdg_shell_xml() {
    if [[ -n "${XR_XDG_SHELL_XML:-}" ]]; then
        if [[ -f "$XR_XDG_SHELL_XML" ]]; then
            printf '%s\n' "$XR_XDG_SHELL_XML"
            return 0
        fi
        echo "missing dependency: XR_XDG_SHELL_XML=$XR_XDG_SHELL_XML" >&2
        return 1
    fi

    local candidate protocol_data
    local candidates=()
    # Keep native UI regression tests self-contained. This is the upstream
    # wayland-protocols 1.19 stable xdg-shell XML (MIT; license is in the file).
    candidates+=("$directory/protocols/xdg-shell.xml")
    if [[ -n "${sysroot:-}" ]]; then
        candidates+=("$sysroot/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml")
    fi
    if command -v pkg-config >/dev/null 2>&1; then
        protocol_data="$(pkg-config --variable=pkgdatadir wayland-protocols 2>/dev/null || true)"
        if [[ -n "$protocol_data" ]]; then
            candidates+=("$protocol_data/stable/xdg-shell/xdg-shell.xml")
        fi
    fi
    candidates+=(/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml)
    for candidate in "${candidates[@]}"; do
        if [[ -f "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    echo "missing dependency: xdg-shell.xml (install wayland-protocols or set XR_XDG_SHELL_XML)" >&2
    return 1
}

protocol="$(find_xdg_shell_xml)" || exit 2

for resource in "$lvgl_root/include/lvgl/lvgl.h" "$lvgl_library" "$protocol"; do
    [ -f "$resource" ] || { echo "missing dependency: $resource" >&2; exit 2; }
done
# PC 侧的 wayland 头文件也要在(生成绑定要用 wayland-scanner + 头文件);
# 缺了应该报"missing dependency", 而不是后面 cp 报一堆费解的错误。
for header in /usr/include/wayland-client.h /usr/include/wayland-util.h; do
    [ -f "$header" ] || { echo "missing dependency: $header (装 wayland 客户端开发头)" >&2; exit 2; }
done
command -v wayland-scanner >/dev/null 2>&1 \
    || { echo "missing dependency: wayland-scanner (装 wayland 工具)" >&2; exit 2; }
build="$directory/build/$mode"
mkdir -p "$build/include" "$build/lib"
cp /usr/include/wayland-*.h "$build/include/"
wayland-scanner client-header "$protocol" "$build/include/xdg-shell-client-protocol.h"
wayland-scanner private-code "$protocol" "$build/xdg-shell-protocol.c"
cp "$lvgl_library" "$build/lib/liblvgl.so"
"$compiler" -std=c11 -O2 -g -Wall -Wextra -Werror \
    -DLV_CONF_INCLUDE_SIMPLE -DLV_DRV_NO_CONF \
    -I"$build/include" -I"$lvgl_root/include" -I"$lvgl_root/include/lvgl" \
    -I"$directory" "${flags[@]}" \
    "$directory/main.c" "$directory/wayland_port.c" "$directory/ui_client.c" \
    "$build/xdg-shell-protocol.c" -L"$build/lib" -llvgl "$wayland_library" \
    -Wl,-rpath,'$ORIGIN/lib' -lm -lrt -lpthread -o "$build/xiaorui_ui"
printf 'Built: %s\n' "$build/xiaorui_ui"
