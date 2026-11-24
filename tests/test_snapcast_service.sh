#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SERVICE=$ROOT/ports/rk3562/snapcast-service.sh
TMP=$(mktemp -d)
cleanup() {
    if [ -r "$TMP/cleanup-pids" ]; then
        while IFS= read -r pid; do
            case "$pid" in ''|*[!0-9]*) continue ;; esac
            kill "$pid" 2>/dev/null || true
        done < "$TMP/cleanup-pids"
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT HUP INT TERM

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

# A live Snapclient must be repointed when its cached server disappears and a
# different advertised server becomes reachable. Use isolated fake processes;
# this test never touches an installed service or audio device.
FAIL=$TMP/failover
mkdir -p "$FAIL/log" "$FAIL/run"
cat >"$FAIL/service.env" <<EOF_FAIL
XR_SNAPCAST_ROLE=client
XR_SNAPCAST_SERVER_HOST=auto
XR_SNAPCAST_SERVER_PORT=1704
XR_SNAPCLIENT_BIN=$FAIL/snapclient
XR_SNAPCAST_PYTHON=$FAIL/fake-python
XR_SNAPCAST_BRIDGE=$FAIL/bridge.py
XR_SNAPCAST_RAW_FIFO=$FAIL/music.raw
XR_MUSIC_FIFO=$FAIL/music.fifo
XR_SNAPCAST_DISCOVERY_PYTHON=$FAIL/fake-python
XR_SNAPCAST_DISCOVERY_SCRIPT=$FAIL/discover.py
EOF_FAIL
cat >"$FAIL/fake-python" <<'FAKE_FAIL'
#!/usr/bin/python3
import sys, time
args = sys.argv[1:]
if args and args[0] == "-c":
    sys.exit(1 if len(args) > 2 and args[2] == "192.0.2.40" else 0)
if args and args[0].endswith("discover.py"):
    print("192.0.2.41")
    sys.exit(0)
if args and args[0].endswith("bridge.py"):
    time.sleep(300)
FAKE_FAIL
cat >"$FAIL/snapclient" <<'FAKE_CLIENT'
#!/usr/bin/python3
import time
time.sleep(300)
FAKE_CLIENT
cat >"$FAIL/bridge.py" <<'FAKE_BRIDGE'
# The fake Python wrapper holds this command open for the service test.
FAKE_BRIDGE
: >"$FAIL/discover.py"
cat >"$FAIL/xiaorui_live.c" <<'FAKE_FRONTEND'
#include <unistd.h>
int main(void) { sleep(300); return 0; }
FAKE_FRONTEND
chmod +x "$FAIL/fake-python" "$FAIL/snapclient"
cc "$FAIL/xiaorui_live.c" -o "$FAIL/xiaorui_live"
mkfifo "$FAIL/music.raw"
echo 192.0.2.40 > "$FAIL/run/server-host"
"$FAIL/snapclient" tcp://192.0.2.40:1704 & OLD_CLIENT=$!
"$FAIL/fake-python" "$FAIL/bridge.py" "$FAIL/music.raw" "$FAIL/music.fifo" mix & OLD_BRIDGE=$!
XR_MUSIC_FIFO="$FAIL/music.fifo" "$FAIL/xiaorui_live" & FRONTEND=$!
printf '%s\n' "$OLD_CLIENT" "$OLD_BRIDGE" "$FRONTEND" >> "$TMP/cleanup-pids"
echo "$OLD_CLIENT" > "$FAIL/run/snapclient.pid"
echo "$OLD_BRIDGE" > "$FAIL/run/bridge.pid"
XR_SNAPCAST_HOME="$FAIL" XR_SNAPCAST_LOG_DIR="$FAIL/log" XR_SNAPCAST_PID_DIR="$FAIL/run" \
    "$SERVICE" reconcile > "$FAIL/reconcile.out" 2>&1
[ -r "$FAIL/run/snapclient.pid" ] && cat "$FAIL/run/snapclient.pid" >> "$TMP/cleanup-pids"
[ -r "$FAIL/run/bridge.pid" ] && cat "$FAIL/run/bridge.pid" >> "$TMP/cleanup-pids"
grep -q 'music server changed 192.0.2.40 -> 192.0.2.41; restarting client' "$FAIL/reconcile.out"
[ "$(cat "$FAIL/run/server-host")" = 192.0.2.41 ]
[ -p "$FAIL/music.raw" ]
NEW_CLIENT=$(cat "$FAIL/run/snapclient.pid")
NEW_BRIDGE=$(cat "$FAIL/run/bridge.pid")
[ "$NEW_CLIENT" != "$OLD_CLIENT" ]
[ "$NEW_BRIDGE" != "$OLD_BRIDGE" ]
kill "$NEW_CLIENT" "$NEW_BRIDGE" "$OLD_CLIENT" "$OLD_BRIDGE" "$FRONTEND" 2>/dev/null || true
wait "$NEW_CLIENT" "$NEW_BRIDGE" "$OLD_CLIENT" "$OLD_BRIDGE" "$FRONTEND" 2>/dev/null || true
echo 'Snapcast stale-server failover PASS'
