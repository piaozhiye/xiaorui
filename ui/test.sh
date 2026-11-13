#!/bin/bash
set -euo pipefail

directory="$(cd "$(dirname "$0")" && pwd)"
lvgl_root="${XR_LVGL_ROOT:-$directory/third_party/lvgl}"
bash "$directory/build.sh" native
build="$directory/build/native"
"${CC:-gcc}" -std=c11 -O2 -g -Wall -Wextra -Werror \
    -DLV_CONF_INCLUDE_SIMPLE -DLV_DRV_NO_CONF \
    -I"$build/include" -I"$lvgl_root/include" -I"$lvgl_root/include/lvgl" \
    -I"$directory" "$directory/runtime_test.c" "$directory/wayland_port.c" \
    "$directory/ui_client.c" "$build/xdg-shell-protocol.c" \
    -L"$build/lib" -llvgl "${XR_HOST_WAYLAND_LIBRARY:--lwayland-client}" \
    -Wl,-rpath,'$ORIGIN/lib' -lm -lrt -lpthread -o "$build/runtime_test"

test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
export XR_UI_TEST_FONT="${XR_UI_TEST_FONT:-/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc}"
"$build/runtime_test" scroll
"$build/runtime_test" degraded-exit
check_fallback() {
    XR_UI_FONT="$test_dir/missing.otf" "$build/xiaorui_ui" \
        --snapshot "$test_dir/fallback.ppm" "$@"
    test -s "$test_dir/fallback.ppm"
}
check_fallback
check_fallback --settings
check_fallback --demo
check_fallback --settings --demo
echo 'UI font fallback regressions passed'
