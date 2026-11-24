#!/bin/sh
# Restart the RK3562 direct-ALSA Snapclient after the frontend releases the card.
# Intended for XR_SNAPCLIENT_RESTART; this does not stop an existing client.
set -eu

CLIENT=${XR_SNAPCLIENT_BIN:-/data/xiaorui/snapcast/snapclient}
SERVER=${XR_SNAPCAST_SERVER:-}
SOUNDCARD=${XR_SNAPCLIENT_SOUNDCARD:-xr_spk}
LOG=${XR_SNAPCLIENT_LOG:-/tmp/snapclient-yield.log}
STATUS=${XR_SNAPCLIENT_ALSA_STATUS:-/proc/asound/card0/pcm0p/sub0/status}
WAIT_MS=${XR_SNAPCLIENT_READY_TIMEOUT_MS:-5000}

if [ -z "$SERVER" ]; then
    echo "XR_SNAPCAST_SERVER must be host:port (normally host:1704)" >&2
    exit 2
fi
case "$WAIT_MS" in ''|*[!0-9]*) echo "invalid XR_SNAPCLIENT_READY_TIMEOUT_MS: $WAIT_MS" >&2; exit 2;; esac
[ -x "$CLIENT" ] || { echo "snapclient is not executable: $CLIENT" >&2; exit 2; }

# Do not create a second client if this hook is invoked manually or twice.
if pgrep -x snapclient >/dev/null 2>&1; then
    echo "snapclient already running; refusing duplicate start" >&2
    exit 3
fi

mkdir -p "$(dirname "$LOG")"
rm -f "$LOG"
if command -v setsid >/dev/null 2>&1; then
    setsid "$CLIENT" "tcp://$SERVER" --soundcard "$SOUNDCARD" \
        --player alsa --mixer software --logsink stdout --logfilter '*:info' \
        >>"$LOG" 2>&1 </dev/null &
else
    "$CLIENT" "tcp://$SERVER" --soundcard "$SOUNDCARD" \
        --player alsa --mixer software --logsink stdout --logfilter '*:info' \
        >>"$LOG" 2>&1 </dev/null &
fi
CLIENT_PID=$!

elapsed=0
while [ "$elapsed" -lt "$WAIT_MS" ]; do
    if ! kill -0 "$CLIENT_PID" 2>/dev/null; then
        wait "$CLIENT_PID" 2>/dev/null || true
        echo "snapclient exited before ALSA became ready; see $LOG" >&2
        exit 1
    fi
    if grep -q '^state: RUNNING' "$STATUS" 2>/dev/null; then
        echo "snapclient ready pid=$CLIENT_PID card=$SOUNDCARD server=$SERVER"
        exit 0
    fi
    sleep 0.05
    elapsed=$((elapsed + 50))
done

echo "snapclient did not reach ALSA RUNNING within ${WAIT_MS}ms; see $LOG" >&2
kill "$CLIENT_PID" 2>/dev/null || true
wait "$CLIENT_PID" 2>/dev/null || true
exit 1
