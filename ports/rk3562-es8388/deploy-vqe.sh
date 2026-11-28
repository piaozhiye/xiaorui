#!/usr/bin/env bash
# Install the RK3562 AIOT DEMO 2mic+2ref VQE overlay after deploy.sh.
# Usage: XR_VQE_PKG=/path/to/rkaudio_vqe_plugin-20261110 \
#        XR_VQE_LIB_DIR=/path/to/Linux_8.3.0_aarch64 bash deploy-vqe.sh SERIAL
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
PKG="${XR_VQE_PKG:?Set XR_VQE_PKG to the Rockchip rkaudio_vqe_plugin-20261110 package}"
SERIAL="${1:-}"
[ -n "$SERIAL" ] || { echo "Usage: $0 SERIAL" >&2; exit 2; }
ADB=(adb -s "$SERIAL")
die() { echo "ERROR: $*" >&2; exit 1; }

PKG_ROOT="$PKG/rkaudio_vqe_plugin-20261110"
[ -d "$PKG_ROOT/lib/linux-aarch64" ] && PKG="$PKG_ROOT"
LIB_DIR="${XR_VQE_LIB_DIR:-$PKG/lib/linux-aarch64}"
CFG_SRC="$PKG/config/2mic_2ref/config_aivqe_new.json"
PLUGIN="$PKG/lib/linux-aarch64/libasound_module_pcm_rkaudio_vqe.so"
for f in "$CFG_SRC" "$PLUGIN"; do
    [ -f "$f" ] || die "Missing VQE package asset: $f"
done
for f in librkaudio_vqe.so librkaudio_common.so librkaudio_nn.so; do
    [ -f "$LIB_DIR/$f" ] || die "Missing AArch64 VQE library: $LIB_DIR/$f"
done

STATE=$("${ADB[@]}" get-state 2>/dev/null | tr -d '\r')
[ "$STATE" = device ] || die "ADB target $SERIAL is unavailable"
MODEL=$("${ADB[@]}" shell 'cat /proc/device-tree/model' 2>/dev/null | tr -d '\0\r')
case "$MODEL" in *RK3562*) ;; *) die "Not an RK3562 board: $MODEL" ;; esac
CARDS=$("${ADB[@]}" shell 'cat /proc/asound/cards' 2>/dev/null | tr -d '\0\r')
case "$CARDS" in *rockchipes8388*) ;; *) die "ES8388 card is not present" ;; esac

# Require the exact SAI1 hardware loopback controls used by the verified 4ch
# capture. Resolve the card number from /proc/asound/cards; the index can differ
# across boots and must match mixer-es8388.sh rather than assuming card 0.
CARD=$("${ADB[@]}" shell 'cat /proc/asound/cards' \
    | awk '/rockchipes8388/ {print $1; exit}' | tr -d '\r')
case "$CARD" in ''|*[!0-9]*) die "Could not resolve ES8388 ALSA card index: $CARD" ;; esac
for control in \
    'SAI1 SDI0 Loopback Src Select' 'SAI1 SDI0 Loopback Switch' \
    'SAI1 SDI1 Loopback Src Select' 'SAI1 SDI1 Loopback Switch'; do
    "${ADB[@]}" shell "amixer -c '$CARD' cget name='$control'" >/dev/null 2>&1 \
        || die "Missing SAI1 mixer control: $control"
done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 "$DIR/patch-vqe-config.py" "$CFG_SRC" "$TMP/config_aivqe_new.json"
WEIGHT=$(python3 - "$TMP/config_aivqe_new.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['skv_configs']['bf']['param_path'].rsplit('/',1)[-1])
PY
)
[ -f "$PKG/config/2mic_2ref/$WEIGHT" ] || die "Missing BF weight $WEIGHT"

# Save current files on the host before replacing anything. Missing files on a
# clean image are recorded as absent and need no restore.
BACKUP="/tmp/opencode/rk3562-es8388/vqe-2mic2ref-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
for path in /etc/asound.conf /data/xiaorui/xiaorui.env /data/mixer-es8388.sh; do
    if "${ADB[@]}" shell "test -f '$path'" >/dev/null 2>&1; then
        name=${path//\//_}
        "${ADB[@]}" pull "$path" "$BACKUP/$name" >/dev/null
    fi
done
echo "Pre-deploy backup: $BACKUP"

# Stop the frontend before changing SAI routing; loopback selection must be
# stable before the capture PCM is opened.
if "${ADB[@]}" shell 'pgrep -x xiaorui_live >/dev/null 2>&1' >/dev/null 2>&1; then
    "${ADB[@]}" shell 'sh /etc/init.d/S98xiaorui stop'
fi

"${ADB[@]}" shell 'mkdir -p /usr/lib/alsa-lib /usr/share/rkaudio /data/xiaorui'
"${ADB[@]}" push "$PLUGIN" /usr/lib/alsa-lib/ >/dev/null
for f in librkaudio_vqe.so librkaudio_common.so librkaudio_nn.so; do
    "${ADB[@]}" push "$LIB_DIR/$f" /usr/lib/ >/dev/null
done
"${ADB[@]}" push "$TMP/config_aivqe_new.json" /usr/share/rkaudio/config_aivqe_new.json >/dev/null
"${ADB[@]}" push "$PKG/config/2mic_2ref/$WEIGHT" "/usr/share/rkaudio/$WEIGHT" >/dev/null
"${ADB[@]}" push "$DIR/asound-vqe.conf" /etc/asound.conf >/dev/null
"${ADB[@]}" push "$DIR/frontend.env.vqe.example" /data/xiaorui/xiaorui.env >/dev/null
"${ADB[@]}" push "$DIR/mixer-es8388.sh" /data/mixer-es8388.sh >/dev/null
"${ADB[@]}" shell 'chmod 755 /usr/lib/alsa-lib/libasound_module_pcm_rkaudio_vqe.so /usr/lib/librkaudio_*.so /data/mixer-es8388.sh'

# Apply mic/reference routing before any PCM is opened, then smoke-test the
# ALSA virtual device with the configured AEC/BF chain.
"${ADB[@]}" shell 'sh /data/mixer-es8388.sh'
"${ADB[@]}" shell 'arecord -D xr_vqe_16k -r 16000 -c 1 -f S16_LE -d 2 /tmp/xr-vqe-smoke.wav >/tmp/xr-vqe-smoke.log 2>&1' \
    || { "${ADB[@]}" shell 'cat /tmp/xr-vqe-smoke.log' >&2; die "VQE ALSA smoke test failed"; }
"${ADB[@]}" shell 'test -s /tmp/xr-vqe-smoke.wav' || die "VQE smoke test produced no PCM"
"${ADB[@]}" shell 'rm -f /tmp/xr-vqe-smoke.wav /tmp/xr-vqe-smoke.log'

# If the base XiaoRui profile is already deployed, restart it with the VQE
# capture device. A clean board can receive the VQE overlay first and start the
# full profile later via the normal S98 service.
if "${ADB[@]}" shell 'test -x /etc/init.d/S98xiaorui' >/dev/null 2>&1; then
    MARK=$("${ADB[@]}" shell 'wc -l < /data/xiaorui/frontend.log 2>/dev/null' 2>/dev/null | tr -dc '0-9')
    MARK=${MARK:-0}
    "${ADB[@]}" shell 'sh /etc/init.d/S98xiaorui start'
    for _ in $(seq 1 45); do
        if "${ADB[@]}" shell "tail -n +$((MARK+1)) /data/xiaorui/frontend.log 2>/dev/null | grep -q 'mic=xr_vqe_16k 聆听中'"; then
            echo "XiaoRui frontend opened xr_vqe_16k; patched config requests AEC/BF/ANR."
            echo "Inspect plugin initialization separately: adb -s $SERIAL shell 'grep -E \"AEC-RELEASE|BEAMFORM-RELEASE|model_bf_en\" /data/xiaorui/frontend.log | tail'"
            exit 0
        fi
        sleep 2
    done
    "${ADB[@]}" shell 'tail -80 /data/xiaorui/frontend.log' >&2
    die "Frontend did not open xr_vqe_16k"
fi
echo "VQE plugin and ALSA chain installed and smoke-tested; base S98 XiaoRui profile is not installed yet."
echo "Backup: $BACKUP"
