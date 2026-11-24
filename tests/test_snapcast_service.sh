#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SERVICE=$ROOT/ports/rk3562/snapcast-service.sh
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

sh -n "$SERVICE"
mkdir -p "$TMP/log" "$TMP/run"
cat >"$TMP/service.env" <<EOF_CFG
XR_SNAPCAST_ROLE=client
XR_SNAPCAST_SERVER_HOST=auto
XR_SNAPCAST_SERVER_PORT=1704
XR_SNAPCLIENT_BIN=$TMP/snapclient
XR_SNAPCAST_PYTHON=$TMP/fake-python
XR_SNAPCAST_BRIDGE=$TMP/bridge.py
XR_SNAPCAST_RAW_FIFO=$TMP/music.raw
XR_MUSIC_FIFO=$TMP/music.fifo
XR_SNAPCAST_DISCOVERY_PYTHON=$TMP/fake-python
XR_SNAPCAST_DISCOVERY_SCRIPT=$TMP/discover.py
EOF_CFG
: >"$TMP/snapclient"
: >"$TMP/bridge.py"
: >"$TMP/discover.py"
cat >"$TMP/fake-python" <<'FAKE_PY'
#!/bin/sh
if [ "$1" = -c ]; then
    case "$*" in
        *192.0.2.40*) exit "${FAKE_CONNECT_40:-0}" ;;
        *) exit 1 ;;
    esac
fi
echo 192.0.2.41
FAKE_PY
chmod +x "$TMP/fake-python"
chmod +x "$TMP/snapclient"

# A healthy cached address should skip the fixed four-second mDNS discovery.
echo 192.0.2.40 > "$TMP/run/server-host"
OUTPUT=$(XR_SNAPCAST_HOME="$TMP" XR_SNAPCAST_LOG_DIR="$TMP/log" XR_SNAPCAST_PID_DIR="$TMP/run" \
    "$SERVICE" resolve-server 2>&1)
printf '%s\n' "$OUTPUT" | grep -q 'reusing cached music server at 192.0.2.40:1704'
printf '%s\n' "$OUTPUT" | grep -qx '192.0.2.40'

# A stale cached address should fall back to mDNS and atomically replace it.
FAKE_CONNECT_40=1 XR_SNAPCAST_HOME="$TMP" XR_SNAPCAST_LOG_DIR="$TMP/log" XR_SNAPCAST_PID_DIR="$TMP/run" \
    "$SERVICE" resolve-server > "$TMP/out" 2>&1
grep -q 'discovered music server at 192.0.2.41:1704' "$TMP/out"
[ "$(cat "$TMP/run/server-host")" = 192.0.2.41 ]

if XR_SNAPCAST_HOME="$TMP" XR_SNAPCAST_LOG_DIR="$TMP/log" XR_SNAPCAST_PID_DIR="$TMP/run" \
    "$SERVICE" start >"$TMP/out" 2>&1; then
    echo "service unexpectedly started without a frontend music FIFO" >&2
    exit 1
fi
grep -q 'refusing to start' "$TMP/out"
[ ! -e "$TMP/music.raw" ]
[ ! -e "$TMP/run/snapclient.pid" ]
echo 'Snapcast service frontend guard PASS'
