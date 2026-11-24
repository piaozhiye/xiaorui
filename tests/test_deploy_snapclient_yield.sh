#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
mkdir -p "$TMP/bin" "$TMP/state"
cat > "$TMP/bin/adb" <<'ADB'
#!/bin/sh
printf '%s\n' "$*" >> "$ADB_LOG"
[ "$1" = -s ] || exit 90
shift 2
case "$1" in
    get-state) echo device ;;
    shell)
        case "$2" in
            "test -x /data/xiaorui/snapcast/snapclient") exit 0 ;;
            "test -e /data/xiaorui/snapcast/snapclient-alsa-resume.sh")
                [ -f "$ADB_STATE/exists" ] ;;
            "sha256sum /data/xiaorui/snapcast/snapclient-alsa-resume.sh")
                printf '%s  /data/xiaorui/snapcast/snapclient-alsa-resume.sh\n' "$(cat "$ADB_STATE/sha")" ;;
            "mkdir -p /data/xiaorui/snapcast") exit 0 ;;
            chmod\ 755\ */.snapclient-alsa-resume.sh.new.*\ \&\&\ mv\ *)
                touch "$ADB_STATE/exists"
                cat "$ADB_STATE/pushed_sha" > "$ADB_STATE/sha"
                ;;
            rm\ -f\ */.snapclient-alsa-resume.sh.new.*) exit 0 ;;
            *) echo "unexpected adb shell: $2" >&2; exit 91 ;;
        esac
        ;;
    push)
        [ "$#" -eq 3 ] || exit 94
        sha256sum "$2" | awk '{print $1}' > "$ADB_STATE/pushed_sha"
        ;;
    *) echo "unexpected adb operation: $1" >&2; exit 93 ;;
esac
ADB
chmod +x "$TMP/bin/adb"
export PATH="$TMP/bin:$PATH" ADB_LOG="$TMP/adb.log" ADB_STATE="$TMP/state"

# Existing nonmatching content must be preserved; no push may happen.
touch "$TMP/state/exists"
echo deadbeef > "$TMP/state/sha"
set +e
OUTPUT=$("$ROOT/ports/rk3562/deploy-snapclient-yield.sh" test-board 2>&1)
STATUS=$?
set -e
[ "$STATUS" -eq 3 ] || { echo "expected conflict exit 3, got $STATUS" >&2; echo "$OUTPUT" >&2; exit 1; }
printf '%s\n' "$OUTPUT" | grep -q 'refusing to overwrite different board file'
if grep -q ' push ' "$ADB_LOG"; then echo 'installer pushed despite conflict' >&2; exit 1; fi
rm -f "$TMP/state/exists" "$TMP/state/sha"

# Missing content is installed and verified, then the second run is idempotent.
OUTPUT=$("$ROOT/ports/rk3562/deploy-snapclient-yield.sh" test-board 2>&1)
printf '%s\n' "$OUTPUT" | grep -q '^installed inert resume hook'
PUSHES=$(grep -c ' push ' "$ADB_LOG")
[ "$PUSHES" -eq 1 ] || { echo "expected one push, got $PUSHES" >&2; exit 1; }
OUTPUT=$("$ROOT/ports/rk3562/deploy-snapclient-yield.sh" test-board 2>&1)
printf '%s\n' "$OUTPUT" | grep -q '^resume hook already current'
PUSHES_AFTER=$(grep -c ' push ' "$ADB_LOG")
[ "$PUSHES_AFTER" -eq 1 ] || { echo "idempotent run pushed again ($PUSHES_AFTER)" >&2; exit 1; }
printf '%s\n' 'deploy installer tests: PASS (conflict guard, verified install, idempotence)'
