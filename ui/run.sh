#!/bin/sh
set -eu

directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XR_UI_FONT="${XR_UI_FONT:-/usr/share/fonts/source-han-sans-cn/SourceHanSansCN-Regular.otf}"
export XR_UI_SOCKET="${XR_UI_SOCKET:-/run/xiaorui/frontend.sock}"
export LD_LIBRARY_PATH="$directory/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$directory/xiaorui_ui" "$@"
