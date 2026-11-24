#!/bin/sh
# Install the inert Snapclient resume hook on one explicitly selected RK3562.
# This does not edit xiaorui.env, restart services, or start audio.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <adb-serial>" >&2
    exit 2
fi
SERIAL=$1
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE=$DIR/snapclient-alsa-resume.sh
REMOTE_DIR=/data/xiaorui/snapcast
REMOTE=$REMOTE_DIR/snapclient-alsa-resume.sh
REMOTE_TMP=$REMOTE_DIR/.snapclient-alsa-resume.sh.new.$$

command -v adb >/dev/null 2>&1 || { echo "adb is required" >&2; exit 2; }
[ -x "$SOURCE" ] || { echo "missing executable source: $SOURCE" >&2; exit 2; }
adb -s "$SERIAL" get-state 2>/dev/null | grep -qx device \
    || { echo "device unavailable: $SERIAL" >&2; exit 2; }
adb -s "$SERIAL" shell "test -x $REMOTE_DIR/snapclient" \
    || { echo "board Snapclient missing: $REMOTE_DIR/snapclient" >&2; exit 2; }

LOCAL_SHA=$(sha256sum "$SOURCE" | awk '{print $1}')
if adb -s "$SERIAL" shell "test -e $REMOTE"; then
    REMOTE_SHA=$(adb -s "$SERIAL" shell "sha256sum $REMOTE" | awk '{print $1}' | tr -d '\r')
    if [ "$REMOTE_SHA" = "$LOCAL_SHA" ]; then
        echo "resume hook already current on $SERIAL ($LOCAL_SHA)"
        exit 0
    fi
    echo "refusing to overwrite different board file: $REMOTE" >&2
    echo "board SHA256=$REMOTE_SHA local SHA256=$LOCAL_SHA" >&2
    exit 3
fi

cleanup() { adb -s "$SERIAL" shell "rm -f $REMOTE_TMP" >/dev/null 2>&1 || true; }
trap cleanup EXIT HUP INT TERM
adb -s "$SERIAL" shell "mkdir -p $REMOTE_DIR"
adb -s "$SERIAL" push "$SOURCE" "$REMOTE_TMP" >/dev/null
adb -s "$SERIAL" shell "chmod 755 $REMOTE_TMP && mv $REMOTE_TMP $REMOTE"
REMOTE_SHA=$(adb -s "$SERIAL" shell "sha256sum $REMOTE" | awk '{print $1}' | tr -d '\r')
[ "$REMOTE_SHA" = "$LOCAL_SHA" ] || {
    echo "board SHA256 mismatch after install: $REMOTE_SHA" >&2
    exit 1
}
echo "installed inert resume hook on $SERIAL: $REMOTE ($LOCAL_SHA)"
echo "No environment variables, services, playback, or mixer settings were changed."
