#!/bin/bash
# Host-side runner for a fixed repeated XR_MIC FIFO corpus.
set -euo pipefail

usage() {
    echo "usage: $0 <adb-serial> <fixture.raw> <expected-trials> [expected-asr-text] [output-dir] [startup-silence-ms] [tail-silence-ms]" >&2
    exit 2
}

[[ $# -ge 3 && $# -le 7 ]] || usage
serial=$1
fixture=$(realpath "$2")
trials=$3
expected_asr=${4:-现在几点了}
out=${5:-"/tmp/kws-e2e-$(date +%Y%m%d-%H%M%S)"}
startup_silence_ms=${6:-5000}
tail_silence_ms=${7:-1000}
[[ $trials =~ ^[1-9][0-9]*$ ]] || usage
[[ $startup_silence_ms =~ ^(0|[1-9][0-9]*)$ ]] && (( startup_silence_ms % 100 == 0 )) || usage
[[ $tail_silence_ms =~ ^(0|[1-9][0-9]*)$ ]] && (( tail_silence_ms % 100 == 0 )) || usage
[[ -r $fixture ]] || { echo "fixture not readable: $fixture" >&2; exit 2; }
command -v adb >/dev/null || { echo "adb not found" >&2; exit 2; }
mkdir -p "$out"
fixture_bytes=$(stat -c %s "$fixture")
(( fixture_bytes > 0 && fixture_bytes % 2 == 0 )) || {
    echo "fixture must contain non-empty S16LE data with an even byte count" >&2; exit 2;
}
(( fixture_bytes % (2 * trials) == 0 )) || {
    echo "fixture size is not an integer number of S16LE frames per expected trial" >&2; exit 2;
}
fixture_sha=$(sha256sum "$fixture" | awk '{print $1}')
tail_align_bytes=$(((3200 - fixture_bytes % 3200) % 3200))
feed_tail_bytes=$((tail_align_bytes + 32 * tail_silence_ms))
remote_fixture=/tmp/kws-e2e-fixture.raw

adb -s "$serial" get-state | grep -qx device || { echo "ADB device is not ready: $serial" >&2; exit 1; }
echo "fixture=$fixture bytes=$fixture_bytes frames=$((fixture_bytes / 2)) frames_per_trial=$((fixture_bytes / (2 * trials))) sha256=$fixture_sha"
echo "trials=$trials expected_asr=$expected_asr startup_silence_ms=$startup_silence_ms tail_silence_ms=$tail_silence_ms serial=$serial output=$out"

adb -s "$serial" push "$fixture" "$remote_fixture" >/dev/null
adb -s "$serial" push "$(dirname "$(realpath "$0")")/mic-feed.py" /data/xiaorui/mic-feed.py >/dev/null
adb -s "$serial" push "$(dirname "$(realpath "$0")")/board-start-kws.sh" /data/xiaorui/start-kws.sh >/dev/null
remote_sha=$(adb -s "$serial" shell "sha256sum '$remote_fixture'" | tr -d '\r' | awk '{print $1}')
[[ $remote_sha == "$fixture_sha" ]] || {
    echo "fixture hash mismatch: local=$fixture_sha board=$remote_sha" >&2; exit 1;
}
adb -s "$serial" shell 'test -x /data/gwvenv/bin/python && test -x /data/xiaorui/bin/xiaorui_live && test -r /data/xiaorui/xiaorui.env'
frontend_sha=$(adb -s "$serial" shell 'sha256sum /data/xiaorui/bin/xiaorui_live' | tr -d '\r' | awk '{print $1}')
echo "board_frontend_sha256=$frontend_sha"

restore_frontend() {
    echo "Restoring normal frontend on $serial" >&2
    adb -s "$serial" shell 'pkill -f "[m]ic-feed.py /tmp/kws-e2e-fixture.raw" 2>/dev/null || true; pkill -x xiaorui_live 2>/dev/null || true; sleep 1; setsid sh /data/xiaorui/start-frontend.sh >>/data/xiaorui/frontend.log 2>&1 </dev/null &' >/dev/null 2>&1 || true
}
trap restore_frontend EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

adb -s "$serial" shell "sh /data/xiaorui/start-kws.sh '$remote_fixture' 1 '$startup_silence_ms' '$tail_silence_ms'"
deadline=$((SECONDS + 2400))
while (( SECONDS < deadline )); do
    if adb -s "$serial" shell "grep -q '^feed_error ' /tmp/mic-feed.log" >/dev/null 2>&1; then
        adb -s "$serial" pull /tmp/mic-feed.log "$out/mic-feed.log" >/dev/null || true
        echo "board feeder reported an error; see $out/mic-feed.log" >&2
        exit 1
    fi
    if ! adb -s "$serial" shell 'pgrep -x xiaorui_live >/dev/null' >/dev/null 2>&1; then
        adb -s "$serial" pull /data/xiaorui/frontend.log "$out/frontend.log" >/dev/null 2>&1 || true
        adb -s "$serial" pull /tmp/mic-feed.log "$out/mic-feed.log" >/dev/null 2>&1 || true
        echo "frontend exited before fixture feed completed; partial logs saved under $out" >&2
        exit 1
    fi
    if adb -s "$serial" shell "grep -q '^feed_done ' /tmp/mic-feed.log" >/dev/null 2>&1; then
        break
    fi
    sleep 20
done
if (( SECONDS >= deadline )); then
    echo "timed out waiting for feeder completion" >&2
    exit 1
fi

echo "Feeder complete; allowing up to 45s for final backend replies."
sleep 45
adb -s "$serial" pull /data/xiaorui/frontend.log "$out/frontend.log" >/dev/null
adb -s "$serial" pull /tmp/mic-feed.log "$out/mic-feed.log" >/dev/null
adb -s "$serial" shell "sha256sum '$remote_fixture' /data/xiaorui/bin/xiaorui_live; wc -c '$remote_fixture'; pgrep -x xiaorui_live >/dev/null && echo frontend_alive || echo frontend_down; free -m | head -2" > "$out/board-state.txt" 2>&1
printf 'fixture_sha256=%s\nfrontend_sha256=%s\nfixture_bytes=%s\nfeed_tail_bytes=%s\nframes_per_trial=%s\nexpected_trials=%s\nexpected_asr=%s\nstartup_silence_ms=%s\ntail_silence_ms=%s\nserial=%s\n' \
    "$fixture_sha" "$frontend_sha" "$fixture_bytes" "$feed_tail_bytes" "$((fixture_bytes / (2 * trials)))" "$trials" "$expected_asr" "$startup_silence_ms" "$tail_silence_ms" "$serial" > "$out/run-meta.txt"

python3 -I "$(dirname "$(realpath "$0")")/analyze-kws-e2e.py" \
    "$out/frontend.log" "$trials" --expected-asr "$expected_asr" \
    --feed-log "$out/mic-feed.log" --expected-feed-sha256 "$fixture_sha" \
    --expected-feed-bytes "$fixture_bytes" --expected-feed-loops 1 \
    --expected-feed-preroll-bytes "$((startup_silence_ms * 32))" \
    --expected-feed-tail-bytes "$feed_tail_bytes" \
    | tee "$out/analysis.txt"
