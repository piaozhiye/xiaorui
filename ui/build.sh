#!/bin/bash
set -euo pipefail

directory="$(cd "$(dirname "$0")" && pwd)"
lvgl_root="${XR_LVGL_ROOT:?Set XR_LVGL_ROOT to the LVGL 8.4 SDK with matching include/ and lib/}"
mode="${1:-aarch64}"
case "$mode" in
    aarch64)
        toolchain="${XR_TOOLCHAIN_ROOT:-$HOME/projects/toolchains/gcc-arm-buildroot-aarch}"
        compiler="$toolchain/bin/aarch64-buildroot-linux-gnu-gcc"
        sysroot="$toolchain/aarch64-buildroot-linux-gnu/sysroot"
        lvgl_library="${XR_LVGL_LIBRARY:-$lvgl_root/lib/aarch64/liblvgl.so}"
        protocol="${XR_XDG_SHELL_XML:-$sysroot/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml}"
        wayland_library="${XR_WAYLAND_LIBRARY:?Set XR_WAYLAND_LIBRARY to target libwayland-client.so.0}"
        flags=(-march=armv8-a -Wl,--allow-shlib-undefined)
        export LD_LIBRARY_PATH="$toolchain/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        ;;
    native)
        compiler="${CC:-gcc}"
        lvgl_library="${XR_LVGL_LIBRARY:-$lvgl_root/lib/x86/liblvgl.so}"
        protocol="${XR_XDG_SHELL_XML:?Set XR_XDG_SHELL_XML to stable/xdg-shell/xdg-shell.xml}"
        wayland_library="${XR_HOST_WAYLAND_LIBRARY:--lwayland-client}"
        flags=()
        ;;
    *) echo "usage: $0 [aarch64|native]" >&2; exit 2 ;;
esac

for resource in "$lvgl_root/include/lvgl/lvgl.h" "$lvgl_library" "$protocol"; do
    [ -f "$resource" ] || { echo "missing dependency: $resource" >&2; exit 2; }
done
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
