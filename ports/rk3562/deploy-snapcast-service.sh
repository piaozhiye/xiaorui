#!/bin/sh
# Install the Snapcast runtime manager on one explicitly selected RK3562.
# This stages files and an init hook; it never restarts the voice frontend or plays audio.
set -eu

usage() {
    echo "usage: $0 <adb-serial> <client|server-client> <server-host> <server-bind-or-dash> [snapserver-bundle-root] [music-root] [local-ffmpeg]" >&2
    echo "server bundle root must contain snapserver and lib/ (Snapcast v0.35.0 aarch64)" >&2
    echo "Use server-host=auto on clients for mDNS discovery of the unique LAN music server." >&2
    exit 2
}
[ "$#" -ge 4 ] && [ "$#" -le 7 ] || usage

SERIAL=$1
ROLE=$2
SERVER_HOST=$3
SERVER_BIND=$4
BUNDLE=${5:-}
MUSIC_ROOT=${6:-}
LOCAL_FFMPEG=${7:-}
LOCAL_FFPROBE=${XR_FFPROBE_BIN:-}
if [ -z "$LOCAL_FFPROBE" ]; then
    case "$LOCAL_FFMPEG" in
        */*) LOCAL_FFPROBE=${LOCAL_FFMPEG%/*}/ffprobe ;;
        *) LOCAL_FFPROBE=ffprobe ;;
    esac
fi
case "$ROLE" in client|server-client) ;; *) usage ;; esac
case "$SERVER_HOST" in *[!A-Za-z0-9._-]*|'') echo "invalid server host: $SERVER_HOST" >&2; exit 2;; esac
case "$SERVER_BIND" in -) ;; *[!0-9.]*|'') echo "invalid bind IPv4: $SERVER_BIND" >&2; exit 2;; esac
if [ "$ROLE" = server-client ]; then
    [ -n "$BUNDLE" ] && [ -x "$BUNDLE/snapserver" ] && [ -d "$BUNDLE/lib" ] || {
        echo "server-client requires a Snapserver bundle root" >&2; exit 2;
    }
    MACHINE=$(readelf -h "$BUNDLE/snapserver" 2>/dev/null | sed -n 's/.*Machine:[[:space:]]*//p')
    case "$MACHINE" in *AArch64*) ;; *) echo "Snapserver bundle is not AArch64: $MACHINE" >&2; exit 2;; esac
    [ -n "$MUSIC_ROOT" ] && [ -d "$MUSIC_ROOT" ] || { echo "server-client requires a music library directory" >&2; exit 2; }
    [ -n "$LOCAL_FFMPEG" ] && [ -x "$LOCAL_FFMPEG" ] || { echo "server-client requires a local music ffmpeg binary" >&2; exit 2; }
else
    [ "$SERVER_BIND" = - ] || { echo "client role bind must be '-'" >&2; exit 2; }
fi

command -v adb >/dev/null 2>&1 || { echo "adb is required" >&2; exit 2; }
adb -s "$SERIAL" get-state 2>/dev/null | grep -qx device || { echo "device unavailable: $SERIAL" >&2; exit 2; }
REMOTE=/data/xiaorui/snapcast
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

cat >"$TMP/service.env" <<EOF_CFG
XR_SNAPCAST_ROLE=$ROLE
XR_SNAPCAST_SERVER_HOST=$SERVER_HOST
XR_SNAPCAST_SERVER_PORT=1704
XR_SNAPCLIENT_BIN=$REMOTE/snapclient
XR_SNAPSERVER_BIN=$REMOTE/server-v0.35.0/snapserver
XR_SNAPCAST_PYTHON=/data/gwvenv/bin/python
XR_SNAPCAST_BRIDGE=$REMOTE/snapcast-fifo-product.py
XR_SNAPCAST_RAW_FIFO=$REMOTE/music.raw
XR_MUSIC_FIFO=$REMOTE/music.fifo
XR_SNAPSERVER_CONFIG=$REMOTE/server.conf
XR_MUSIC_UI=0
EOF_CFG
if [ "$ROLE" = server-client ]; then
    printf 'XR_SNAPCAST_SERVER_BIND=%s\nXR_MUSIC_UI=1\n' "$SERVER_BIND" >>"$TMP/service.env"
    cat >"$TMP/server.conf" <<EOF_CONF
[server]
mdns_enabled = false
datadir = $REMOTE/server-data
[http]
enabled = true
bind_to_address = 127.0.0.1
port = 1780
doc_root =
publish_http = false
publish_https = false
[tcp-streaming]
enabled = true
bind_to_address = $SERVER_BIND
port = 1704
publish = false
[tcp-control]
enabled = true
bind_to_address = 127.0.0.1
port = 1705
publish = false
[stream]
source = pipe://$REMOTE/default.raw?name=default&mode=create&codec=pcm&sampleformat=48000:16:2
[logging]
sink = stdout
filter = *:info
EOF_CONF
else
    : >"$TMP/server.conf"
fi

echo "== staging Snapcast service on $SERIAL (role=$ROLE, server=$SERVER_HOST:1704) =="
adb -s "$SERIAL" shell "mkdir -p '$REMOTE' '$REMOTE/.install' '$REMOTE/log' '$REMOTE/run'"
adb -s "$SERIAL" push "$DIR/snapcast-service.sh" "$REMOTE/.install/service.sh.new" >/dev/null
adb -s "$SERIAL" shell "chmod 755 '$REMOTE/.install/service.sh.new' && mv '$REMOTE/.install/service.sh.new' '$REMOTE/service.sh'"
adb -s "$SERIAL" push "$DIR/init-S98zzxiaorui-snapcast" "$REMOTE/.install/init-S98zzxiaorui-snapcast.new" >/dev/null
adb -s "$SERIAL" shell "chmod 755 '$REMOTE/.install/init-S98zzxiaorui-snapcast.new' && mv '$REMOTE/.install/init-S98zzxiaorui-snapcast.new' '$REMOTE/init-S98zzxiaorui-snapcast'"
adb -s "$SERIAL" push "$DIR/../../tools/snapcast-fifo-channel.py" "$REMOTE/.install/snapcast-fifo-product.py.new" >/dev/null
adb -s "$SERIAL" shell "chmod 644 '$REMOTE/.install/snapcast-fifo-product.py.new' && mv '$REMOTE/.install/snapcast-fifo-product.py.new' '$REMOTE/snapcast-fifo-product.py'"
adb -s "$SERIAL" shell "mkdir -p /data/xiaorui-server/.install"
adb -s "$SERIAL" push "$DIR/../../board-server/mdns-ha.py" "/data/xiaorui-server/.install/mdns-ha.py.new" >/dev/null
adb -s "$SERIAL" shell "chmod 755 /data/xiaorui-server/.install/mdns-ha.py.new && mv /data/xiaorui-server/.install/mdns-ha.py.new /data/xiaorui-server/mdns-ha.py"
adb -s "$SERIAL" push "$DIR/../../board-server/discover-xiaorui-music.py" "$REMOTE/.install/discover-xiaorui-music.py.new" >/dev/null
adb -s "$SERIAL" shell "chmod 755 '$REMOTE/.install/discover-xiaorui-music.py.new' && mv '$REMOTE/.install/discover-xiaorui-music.py.new' '$REMOTE/discover-xiaorui-music.py'"

if [ "$ROLE" = server-client ]; then
    if adb -s "$SERIAL" shell "test -e '$REMOTE/server-v0.35.0/snapserver'"; then
        LOCAL_SHA=$(sha256sum "$BUNDLE/snapserver" | awk '{print $1}')
        REMOTE_SHA=$(adb -s "$SERIAL" shell "sha256sum '$REMOTE/server-v0.35.0/snapserver'" 2>/dev/null | awk '{print $1}' | tr -d '\r')
        [ "$LOCAL_SHA" = "$REMOTE_SHA" ] || {
            echo "refusing to replace a different installed Snapserver ($REMOTE_SHA != $LOCAL_SHA)" >&2; exit 3;
        }
        echo "Snapserver binary already installed ($LOCAL_SHA)"
    else
        adb -s "$SERIAL" shell "rm -rf '$REMOTE/.install/server-v0.35.0' && mkdir -p '$REMOTE/.install/server-v0.35.0/lib'"
        adb -s "$SERIAL" push "$BUNDLE/snapserver" "$REMOTE/.install/server-v0.35.0/snapserver" >/dev/null
        adb -s "$SERIAL" push "$BUNDLE/lib/." "$REMOTE/.install/server-v0.35.0/lib/" >/dev/null
        adb -s "$SERIAL" shell "chmod 755 '$REMOTE/.install/server-v0.35.0/snapserver' && mv '$REMOTE/.install/server-v0.35.0' '$REMOTE/server-v0.35.0'"
    fi
    adb -s "$SERIAL" shell "mkdir -p '$REMOTE/music-app' '$REMOTE/ffmpeg-local' '$REMOTE/music-state' /data/xiaorui/music-library"
    for file in server.py music_library.py music_collections.py music_session.py snapcast_control.py pairing_auth.py music_ui.html music_manifest.webmanifest music_service_worker.js music_icon.svg music_icon_192.png music_icon_512.png; do
        adb -s "$SERIAL" push "$DIR/../../tools/snapcast_center/$file" "$REMOTE/.install/$file.new" >/dev/null
        adb -s "$SERIAL" shell "chmod 644 '$REMOTE/.install/$file.new' && mv '$REMOTE/.install/$file.new' '$REMOTE/music-app/$file'"
    done
    adb -s "$SERIAL" shell "mkdir -p '$REMOTE/music-app/pyqrcode' '$REMOTE/.install/pyqrcode'"
    for file in __init__.py builder.py tables.py; do
        adb -s "$SERIAL" push "$DIR/../../tools/snapcast_center/pyqrcode/$file" "$REMOTE/.install/pyqrcode/$file.new" >/dev/null
        adb -s "$SERIAL" shell "chmod 644 '$REMOTE/.install/pyqrcode/$file.new' && mv '$REMOTE/.install/pyqrcode/$file.new' '$REMOTE/music-app/pyqrcode/$file'"
    done
    adb -s "$SERIAL" push "$LOCAL_FFMPEG" "$REMOTE/.install/ffmpeg.new" >/dev/null
    adb -s "$SERIAL" shell "chmod 755 '$REMOTE/.install/ffmpeg.new' && mv '$REMOTE/.install/ffmpeg.new' '$REMOTE/ffmpeg-local/ffmpeg'"
    if [ -x "$LOCAL_FFPROBE" ]; then
        adb -s "$SERIAL" push "$LOCAL_FFPROBE" "$REMOTE/.install/ffprobe.new" >/dev/null
        adb -s "$SERIAL" shell "chmod 755 '$REMOTE/.install/ffprobe.new' && mv '$REMOTE/.install/ffprobe.new' '$REMOTE/ffmpeg-local/ffprobe'"
    else
        echo "warning: ffprobe not found at $LOCAL_FFPROBE; artist/album tags, duration metadata, and embedded covers may be unavailable"
    fi
    if ! adb -s "$SERIAL" shell "test -f /data/xiaorui/music-library/.xr-library-installed"; then
        tar -C "$MUSIC_ROOT" -cf - . | adb -s "$SERIAL" shell "tar -xf - -C /data/xiaorui/music-library"
        adb -s "$SERIAL" shell "touch /data/xiaorui/music-library/.xr-library-installed"
    else
        echo "music library marker exists; preserving installed library"
    fi
fi

for file in service.env server.conf; do
    if adb -s "$SERIAL" shell "test -e '$REMOTE/$file'"; then
        LOCAL_SHA=$(sha256sum "$TMP/$file" | awk '{print $1}')
        REMOTE_SHA=$(adb -s "$SERIAL" shell "sha256sum '$REMOTE/$file'" | awk '{print $1}' | tr -d '\r')
        [ "$LOCAL_SHA" = "$REMOTE_SHA" ] || {
            echo "refusing to overwrite customized $REMOTE/$file; back it up and review before retrying" >&2; exit 3;
        }
    else
        adb -s "$SERIAL" push "$TMP/$file" "$REMOTE/.$file.new" >/dev/null
        adb -s "$SERIAL" shell "chmod 600 '$REMOTE/.$file.new' && mv '$REMOTE/.$file.new' '$REMOTE/$file'"
    fi
done

HOOK=/etc/init.d/S98zzxiaorui-snapcast
if adb -s "$SERIAL" shell "test -e '$HOOK'"; then
    if ! adb -s "$SERIAL" shell "grep -q 'Installed after S98xiaorui' '$HOOK'"; then
        echo "unexpected existing init hook: $HOOK; refusing to overwrite" >&2; exit 3
    fi
else
    adb -s "$SERIAL" push "$DIR/init-S98zzxiaorui-snapcast" "$REMOTE/.install/init-hook.new" >/dev/null
    adb -s "$SERIAL" shell "chmod 755 '$REMOTE/.install/init-hook.new' && mv '$REMOTE/.install/init-hook.new' '$HOOK'"
fi

echo "staged. No service was started, no frontend restarted, and no audio/mixer setting changed."
echo "Enable XR_MUSIC_FIFO in the frontend environment before starting the service."
echo "  adb -s $SERIAL shell '$REMOTE/service.sh start'"
