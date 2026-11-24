#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SCRIPT="$ROOT/ports/rk3562/snapclient-alsa-resume.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

mkdir -p "$TMP/bin" "$TMP/proc"
cat >"$TMP/bin/pgrep" <<'EOF'
#!/bin/sh
[ "${XR_TEST_DUPLICATE:-0}" = 1 ] && exit 0
exit 1
EOF
cat >"$TMP/bin/fake-snapclient" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" > "$XR_TEST_ARGS"
echo $$ > "$XR_TEST_PID"
exec sleep 30
EOF
cat >"$TMP/bin/fake-ready-snapclient" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" > "$XR_TEST_ARGS"
echo 'state: RUNNING' > "$XR_TEST_STATUS"
echo $$ > "$XR_TEST_PID"
exec sleep 30
EOF
chmod +x "$TMP/bin/pgrep" "$TMP/bin/fake-snapclient" "$TMP/bin/fake-ready-snapclient"

export PATH="$TMP/bin:/usr/bin:/bin"
export XR_TEST_ARGS="$TMP/args"
export XR_TEST_PID="$TMP/pid"
export XR_TEST_STATUS="$TMP/proc/status"
export XR_SNAPCAST_SERVER=192.0.2.10:1704
export XR_SNAPCLIENT_ALSA_STATUS="$TMP/proc/status"
export XR_SNAPCLIENT_LOG="$TMP/snapclient.log"
export XR_SNAPCLIENT_READY_TIMEOUT_MS=150

# Missing server fails closed before spawning a process.
if env -u XR_SNAPCAST_SERVER XR_SNAPCLIENT_BIN="$TMP/bin/fake-snapclient" \
    XR_SNAPCLIENT_ALSA_STATUS="$TMP/proc/status" "$SCRIPT" >/dev/null 2>&1; then
    echo "expected missing-server failure" >&2; exit 1
fi

# Never start a second client when the frontend hook is invoked twice.
if XR_TEST_DUPLICATE=1 XR_SNAPCLIENT_BIN="$TMP/bin/fake-snapclient" \
    "$SCRIPT" >/dev/null 2>&1; then
    echo "expected duplicate-client refusal" >&2; exit 1
fi
[ ! -e "$TMP/args" ] || { echo "duplicate guard spawned a client" >&2; exit 1; }

# A client that does not acquire ALSA must be stopped, and report failure.
if XR_SNAPCLIENT_BIN="$TMP/bin/fake-snapclient" "$SCRIPT" >/dev/null 2>&1; then
    echo "expected readiness timeout" >&2; exit 1
fi
PID=$(cat "$TMP/pid")
sleep 0.1
if kill -0 "$PID" 2>/dev/null; then
    echo "timed-out fake client leaked (pid=$PID)" >&2; exit 1
fi

# Readiness succeeds, and the exact direct-ALSA/software-mixer argv is used.
XR_SNAPCLIENT_BIN="$TMP/bin/fake-ready-snapclient" "$SCRIPT" >/dev/null
grep -F 'tcp://192.0.2.10:1704 --soundcard xr_spk --player alsa --mixer software' "$TMP/args" >/dev/null
PID=$(cat "$TMP/pid")
kill "$PID" 2>/dev/null || true
echo "snapclient ALSA resume tests passed"
