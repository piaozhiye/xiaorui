#!/bin/sh
set -eu

CONFIG="${XR_MA_KIOSK_CONFIG:-/data/xiaorui-ui/ma-kiosk.env}"
if [ -r "$CONFIG" ]; then
    # This file is a local deployment setting, not part of the public repo.
    . "$CONFIG"
fi

URL="${XR_MA_URL:-}"
if [ -z "$URL" ]; then
    echo "XR_MA_URL is not configured; set it in $CONFIG" >&2
    exit 2
fi
case "$URL" in
    http://*|https://*) ;;
    *) echo "XR_MA_URL must use http:// or https://" >&2; exit 2 ;;
esac
case "$URL" in
    *[\ \	\'\"\;\|\&\`\$]*) echo "XR_MA_URL contains unsupported characters" >&2; exit 2 ;;
esac
ORIGIN="$(python3 - "$URL" <<'PY'
import re
import sys
from urllib.parse import urlsplit

try:
    parsed = urlsplit(sys.argv[1])
    port = parsed.port
except ValueError:
    raise SystemExit(1)

host = parsed.hostname or ""
if (parsed.scheme not in ("http", "https") or not host
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment):
    raise SystemExit(1)
if ":" in host:
    if not re.fullmatch(r"[0-9A-Fa-f:.]+", host):
        raise SystemExit(1)
    authority = f"[{host}]"
else:
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise SystemExit(1)
    authority = host
if port is not None:
    authority += f":{port}"
print(f"{parsed.scheme}://{authority}")
PY
)" || {
    echo "XR_MA_URL must be an HTTP(S) URL without credentials, query, or fragment" >&2
    exit 2
}

CHROMIUM="${XR_CHROMIUM_BIN:-/usr/bin/chromium}"
if [ ! -x "$CHROMIUM" ]; then
    echo "Chromium is not executable: $CHROMIUM" >&2
    exit 127
fi

BASE="${XR_MA_KIOSK_DATA_DIR:-/userdata/xiaorui/music-assistant}"
PROFILE="$BASE/profile"
EXTENSION="$BASE/return-extension"
mkdir -p "$PROFILE" "$EXTENSION"
chmod 700 "$BASE" "$PROFILE" "$EXTENSION"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cp "$SCRIPT_DIR/ma-kiosk-extension/background.js" "$EXTENSION/background.js"
cp "$SCRIPT_DIR/ma-kiosk-extension/content.js" "$EXTENSION/content.js"
sed "s|__XR_MA_ORIGIN__|$ORIGIN|g" \
    "$SCRIPT_DIR/ma-kiosk-extension/manifest.json" > "$EXTENSION/manifest.json"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
exec "$CHROMIUM" \
    --no-sandbox \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-infobars \
    --disable-translate \
    --lang=zh-CN \
    --user-data-dir="$PROFILE" \
    --disable-extensions-except="$EXTENSION" \
    --load-extension="$EXTENSION" \
    --app="$URL"
