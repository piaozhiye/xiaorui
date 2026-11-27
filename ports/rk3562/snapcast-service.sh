#!/bin/sh
# RK3562 Snapcast lifecycle manager. Installed as /data/xiaorui/snapcast/service.sh
# and called by /etc/init.d/S98xiaorui-snapcast after the frontend.
set -eu

BASE=${XR_SNAPCAST_HOME:-/data/xiaorui/snapcast}
CONFIG=${XR_SNAPCAST_CONFIG:-$BASE/service.env}
LOG_DIR=${XR_SNAPCAST_LOG_DIR:-/data/xiaorui/snapcast/log}
PID_DIR=${XR_SNAPCAST_PID_DIR:-/data/xiaorui/snapcast/run}

[ -r "$CONFIG" ] || { echo "missing config: $CONFIG" >&2; exit 2; }
# shellcheck disable=SC1090
. "$CONFIG"

ROLE=${XR_SNAPCAST_ROLE:-client}
SERVER_HOST=${XR_SNAPCAST_SERVER_HOST:-}
DISCOVERY_PYTHON=${XR_SNAPCAST_DISCOVERY_PYTHON:-/data/ha-env/bin/python}
DISCOVERY_SCRIPT=${XR_SNAPCAST_DISCOVERY_SCRIPT:-$BASE/discover-xiaorui-music.py}
DISCOVERY_SERVER_ID=${XR_SNAPCAST_SERVER_ID:-}
MDNS_PYTHON=${XR_MDNS_PYTHON:-/data/ha-env/bin/python}
MDNS_HELPER=${XR_MDNS_HELPER:-/data/xiaorui-server/mdns-ha.py}
MDNS_LIBRARY_PATH=${XR_MDNS_LIBRARY_PATH:-/data/miniforge/lib}
SERVER_BIND=${XR_SNAPCAST_SERVER_BIND:-}
SERVER_PORT=${XR_SNAPCAST_SERVER_PORT:-1704}
CLIENT=${XR_SNAPCLIENT_BIN:-/data/xiaorui/snapcast/snapclient}
SERVER=${XR_SNAPSERVER_BIN:-$BASE/server-v0.35.0/snapserver}
PYTHON=${XR_SNAPCAST_PYTHON:-/data/gwvenv/bin/python}
BRIDGE=${XR_SNAPCAST_BRIDGE:-$BASE/snapcast-fifo-product.py}
RAW_FIFO=${XR_SNAPCAST_RAW_FIFO:-$BASE/music.raw}
MUSIC_FIFO=${XR_MUSIC_FIFO:-/data/xiaorui/snapcast/music.fifo}
SERVER_CONFIG=${XR_SNAPSERVER_CONFIG:-$BASE/server.conf}
MUSIC_UI=${XR_MUSIC_UI:-0}
MUSIC_APP=${XR_MUSIC_APP:-$BASE/music-app}
MUSIC_ROOT=${XR_MUSIC_LIBRARY:-/data/xiaorui/music-library}
MUSIC_UPLOAD_ROOT=${XR_MUSIC_UPLOAD_ROOT:-/userdata/xiaorui/music-uploads}
MUSIC_FIFO_ROOT=${XR_MUSIC_FIFO_ROOT:-$BASE/music-pipes}
MUSIC_FFMPEG=${XR_MUSIC_FFMPEG:-$BASE/ffmpeg-local/ffmpeg}
MUSIC_RADIO_FFMPEG=${XR_MUSIC_RADIO_FFMPEG:-/usr/bin/ffmpeg}
MUSIC_UI_PORT=${XR_MUSIC_UI_PORT:-8765}
MUSIC_VOICE_CONTROL_PORT=${XR_MUSIC_VOICE_CONTROL_PORT:-8766}
MUSIC_UI_BIND=${XR_MUSIC_UI_BIND:-127.0.0.1}
MUSIC_UI_TLS_CERT=${XR_MUSIC_UI_TLS_CERT:-}
MUSIC_UI_TLS_KEY=${XR_MUSIC_UI_TLS_KEY:-}
MUSIC_UI_PASSWORD_FILE=${XR_MUSIC_UI_PASSWORD_FILE:-}
MUSIC_UI_PAIRING_STATE=${XR_MUSIC_UI_PAIRING_STATE:-}
MUSIC_UI_PAIRING_CODE=${XR_MUSIC_UI_PAIRING_CODE:-}
MUSIC_UI_PUBLIC_URL=${XR_MUSIC_UI_PUBLIC_URL:-}
MUSIC_UI_ALLOWED_HOSTS=${XR_MUSIC_UI_ALLOWED_HOSTS:-}

case "$ROLE" in server|server-client|client) ;; *) echo "invalid role: $ROLE" >&2; exit 2;; esac
case "$SERVER_PORT" in ''|*[!0-9]*) echo "invalid server port: $SERVER_PORT" >&2; exit 2;; esac

mkdir -p "$LOG_DIR" "$PID_DIR"
SERVER_PID_FILE=$PID_DIR/snapserver.pid
CLIENT_PID_FILE=$PID_DIR/snapclient.pid
BRIDGE_PID_FILE=$PID_DIR/bridge.pid
UI_PID_FILE=$PID_DIR/music-ui.pid
WATCHDOG_PID_FILE=$PID_DIR/watchdog.pid
SERVER_CACHE_FILE=${XR_SNAPCAST_SERVER_CACHE:-$PID_DIR/server-host}
DISCOVERY_RETRY_FILE=$PID_DIR/server-discovery-last-at

log() { echo "[snapcast-service] $*"; }

pid_matches() {
    pid=$1 expected=$2
    [ -r "/proc/$pid/cmdline" ] || return 1
    cmd=$(tr '\000' ' ' 2>/dev/null < "/proc/$pid/cmdline" || true)
    case "$cmd" in *"$expected"*) return 0;; *) return 1;; esac
}

stop_pidfile() {
    file=$1 expected=$2 label=$3
    [ -r "$file" ] || return 0
    pid=$(cat "$file" 2>/dev/null || true)
    case "$pid" in ''|*[!0-9]*) rm -f "$file"; return 0;; esac
    if pid_matches "$pid" "$expected"; then
        kill -TERM "$pid" 2>/dev/null || true
        i=0
        while [ "$i" -lt 40 ] && kill -0 "$pid" 2>/dev/null; do
            sleep 0.1
            i=$((i + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            log "$label PID $pid did not stop; sending KILL"
            kill -KILL "$pid" 2>/dev/null || true
        fi
    else
        log "stale $label pidfile; not touching PID $pid"
    fi
    rm -f "$file"
}

stop_music_ui() {
    stop_pidfile "$UI_PID_FILE" "$MUSIC_APP/server.py" "music UI"
    # The PID file can be stale after an unclean restart. Match the complete
    # controller command line before stopping a live process; never signal a
    # PID just because an old file contains its number.
    if pid=$(find_pid "$MUSIC_APP/server.py --bind $MUSIC_UI_BIND --port $MUSIC_UI_PORT"); then
        log "stopping music UI found by command line PID $pid"
        kill -TERM "$pid" 2>/dev/null || true
        i=0
        while [ "$i" -lt 40 ] && kill -0 "$pid" 2>/dev/null; do
            sleep 0.1
            i=$((i + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            log "music UI PID $pid did not stop; sending KILL"
            kill -KILL "$pid" 2>/dev/null || true
        fi
    fi
}

find_pid() {
    expected=$1
    for d in /proc/[0-9]*; do
        [ -r "$d/cmdline" ] || continue
        pid=${d#/proc/}
        cmd=$(tr '\000' ' ' 2>/dev/null < "$d/cmdline" || true)
        case "$cmd" in *"$expected"*) echo "$pid"; return 0;; esac
    done
    return 1
}

voice_control_ready() {
    "$PYTHON" -c 'import json,sys,urllib.request; r=urllib.request.Request("http://127.0.0.1:"+sys.argv[1]+"/api/voice", data=json.dumps({"action":"status"}).encode(), headers={"Content-Type":"application/json"}); result=json.load(urllib.request.urlopen(r,timeout=1)); sys.exit(0 if result.get("ok") else 1)' \
        "$MUSIC_VOICE_CONTROL_PORT" >/dev/null 2>&1
}

frontend_has_fifo() {
    wanted="XR_MUSIC_FIFO=$MUSIC_FIFO"
    for d in /proc/[0-9]*; do
        [ -r "$d/comm" ] || continue
        [ "$(cat "$d/comm" 2>/dev/null || true)" = xiaorui_live ] || continue
        env=$(tr '\000' '\n' 2>/dev/null < "$d/environ" || true)
        printf '%s\n' "$env" | grep -Fqx "$wanted" && return 0
    done
    return 1
}

wait_port() {
    host=$1 port=$2
    i=0
    while [ "$i" -lt 100 ]; do
        if "$PYTHON" -c 'import socket,sys; s=socket.socket(); s.settimeout(.2); r=s.connect_ex((sys.argv[1],int(sys.argv[2]))); s.close(); sys.exit(0 if r == 0 else 1)' "$host" "$port" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.1
        i=$((i + 1))
    done
    return 1
}

server_port_open() {
    host=$1
    "$PYTHON" -c 'import socket,sys; s=socket.socket(); s.settimeout(.25); r=s.connect_ex((sys.argv[1],int(sys.argv[2]))); s.close(); sys.exit(0 if r == 0 else 1)' \
        "$host" "$SERVER_PORT" >/dev/null 2>&1
}

cache_server_host() {
    host=$1
    case "$host" in ''|*[!0-9.]*) return 1 ;; esac
    tmp=$SERVER_CACHE_FILE.$$
    printf '%s\n' "$host" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$SERVER_CACHE_FILE"
}

resolve_server_host() {
    case "$SERVER_HOST" in
        auto|discover)
            cached_host=
            if [ -r "$SERVER_CACHE_FILE" ]; then
                cached_host=$(cat "$SERVER_CACHE_FILE" 2>/dev/null || true)
            fi
            case "$cached_host" in ''|*[!0-9.]*) cached_host= ;; esac
            if [ -n "$cached_host" ] && server_port_open "$cached_host"; then
                SERVER_HOST=$cached_host
                log "reusing cached music server at $SERVER_HOST:$SERVER_PORT"
                return 0
            fi
            [ -x "$DISCOVERY_PYTHON" ] || { log "mDNS discovery Python missing: $DISCOVERY_PYTHON"; return 1; }
            [ -r "$DISCOVERY_SCRIPT" ] || { log "mDNS discovery helper missing: $DISCOVERY_SCRIPT"; return 1; }
            if [ -n "$DISCOVERY_SERVER_ID" ]; then
                SERVER_HOST=$("$DISCOVERY_PYTHON" "$DISCOVERY_SCRIPT" --server-id "$DISCOVERY_SERVER_ID") || return 1
            else
                SERVER_HOST=$("$DISCOVERY_PYTHON" "$DISCOVERY_SCRIPT") || return 1
            fi
            cache_server_host "$SERVER_HOST" || {
                log "discovery returned an invalid IPv4 host: $SERVER_HOST"
                return 1
            }
            log "discovered music server at $SERVER_HOST:$SERVER_PORT"
            ;;
    esac
}

start_server() {
    [ -n "$SERVER_BIND" ] || { log "server role requires XR_SNAPCAST_SERVER_BIND"; return 1; }
    [ -x "$SERVER" ] || { log "Snapserver binary missing: $SERVER"; return 1; }
    if pid=$(find_pid "$SERVER -c $SERVER_CONFIG"); then
        echo "$pid" > "$SERVER_PID_FILE"
        log "Snapserver already running PID $pid"
        return 0
    fi
    [ -r "$SERVER_CONFIG" ] || { log "Snapserver config missing: $SERVER_CONFIG"; return 1; }
    mkdir -p "$BASE/server-data"
    setsid env LD_LIBRARY_PATH="$BASE/server-v0.35.0/lib:/usr/lib:/lib" \
        "$SERVER" -c "$SERVER_CONFIG" >>"$LOG_DIR/snapserver.log" 2>&1 </dev/null &
    pid=$!
    echo "$pid" > "$SERVER_PID_FILE"
    if ! wait_port "$SERVER_BIND" 1704; then
        log "Snapserver did not listen on $SERVER_BIND:1704; see $LOG_DIR/snapserver.log"
        stop_pidfile "$SERVER_PID_FILE" "$SERVER" Snapserver
        return 1
    fi
    log "Snapserver ready PID $pid on $SERVER_BIND:1704"
}

start_music_ui() {
    [ "$MUSIC_UI" = 1 ] || return 0
    [ "$ROLE" = server-client ] || { log "music UI requires server-client role"; return 1; }
    [ -x "$PYTHON" ] || { log "Python runtime missing: $PYTHON"; return 1; }
    [ -r "$MUSIC_APP/server.py" ] && [ -r "$MUSIC_APP/music_ui.html" ] || {
        log "music controller files missing under $MUSIC_APP"; return 1;
    }
    [ -d "$MUSIC_ROOT" ] || { log "music library missing: $MUSIC_ROOT"; return 1; }
    [ -x "$MUSIC_FFMPEG" ] || { log "local music ffmpeg missing: $MUSIC_FFMPEG"; return 1; }
    [ -x "$MUSIC_RADIO_FFMPEG" ] || MUSIC_RADIO_FFMPEG=$MUSIC_FFMPEG
    case "$MUSIC_UPLOAD_ROOT" in
        /userdata/*)
            if ! awk '$2 == "/userdata" { found=1 } END { exit !found }' /proc/mounts; then
                log "upload storage is not mounted at /userdata; refusing to start music UI"
                return 1
            fi
            ;;
    esac
    mkdir -p "$MUSIC_UPLOAD_ROOT" || { log "cannot create upload directory: $MUSIC_UPLOAD_ROOT"; return 1; }
    chmod 700 "$MUSIC_UPLOAD_ROOT" || { log "cannot secure upload directory: $MUSIC_UPLOAD_ROOT"; return 1; }
    if pid=$(find_pid "$MUSIC_APP/server.py --bind $MUSIC_UI_BIND --port $MUSIC_UI_PORT"); then
        echo "$pid" > "$UI_PID_FILE"
        if voice_control_ready; then
            log "music UI already running PID $pid (voice control ready)"
            return 0
        fi
        log "existing music UI PID $pid lacks voice control; restarting it"
        stop_music_ui
    fi
    set -- --bind "$MUSIC_UI_BIND" --port "$MUSIC_UI_PORT" \
        --voice-control-port "$MUSIC_VOICE_CONTROL_PORT" \
        --snapcast-host 127.0.0.1 --snapcast-port 1780 \
        --state-file "$BASE/music-state/sessions.json" \
        --queue-state-file "$BASE/music-state/queue.json" \
        --music-root "$MUSIC_ROOT" --music-fifo-root "$MUSIC_FIFO_ROOT" \
        --music-upload-root "$MUSIC_UPLOAD_ROOT" \
        --music-active-file "$BASE/music.active" --ffmpeg "$MUSIC_FFMPEG" \
        --radio-ffmpeg "$MUSIC_RADIO_FFMPEG"
    [ -z "$MUSIC_UI_TLS_CERT" ] || set -- "$@" --tls-cert "$MUSIC_UI_TLS_CERT"
    [ -z "$MUSIC_UI_TLS_KEY" ] || set -- "$@" --tls-key "$MUSIC_UI_TLS_KEY"
    [ -z "$MUSIC_UI_PASSWORD_FILE" ] || set -- "$@" --access-password-file "$MUSIC_UI_PASSWORD_FILE"
    [ -z "$MUSIC_UI_PAIRING_STATE" ] || set -- "$@" --pairing-state-file "$MUSIC_UI_PAIRING_STATE"
    [ -z "$MUSIC_UI_PAIRING_CODE" ] || set -- "$@" --pairing-code-file "$MUSIC_UI_PAIRING_CODE"
    [ -z "$MUSIC_UI_PAIRING_STATE" ] || set -- "$@" --pairing-display-file "${XR_MUSIC_UI_PAIRING_DISPLAY_FILE:-$BASE/music-state/pairing-display.txt}"
    [ -z "$MUSIC_UI_PAIRING_STATE" ] || set -- "$@" --pairing-rotate-request-file "${XR_MUSIC_UI_PAIRING_ROTATE_REQUEST_FILE:-$BASE/music-state/pairing-rotate.request}"
    [ -z "$MUSIC_UI_PUBLIC_URL" ] || set -- "$@" --public-url "$MUSIC_UI_PUBLIC_URL"
    [ -z "$MUSIC_UI_ALLOWED_HOSTS" ] || set -- "$@" --allowed-hosts "$MUSIC_UI_ALLOWED_HOSTS"
    setsid "$PYTHON" "$MUSIC_APP/server.py" "$@" >>"$LOG_DIR/music-ui.log" 2>&1 </dev/null &
    pid=$!
    echo "$pid" > "$UI_PID_FILE"
    i=0
    while [ "$i" -lt 50 ]; do
        if "$PYTHON" -c 'import ssl,sys,urllib.request; ctx=ssl._create_unverified_context(); urllib.request.urlopen(sys.argv[1]+"://127.0.0.1:"+sys.argv[2]+"/health",timeout=.2,context=ctx).read()' "$([ -n "$MUSIC_UI_TLS_CERT" ] && echo https || echo http)" "$MUSIC_UI_PORT" >/dev/null 2>&1; then
            UI_SCHEME=http
            [ -z "$MUSIC_UI_TLS_CERT" ] || UI_SCHEME=https
            log "music UI ready PID $pid at $UI_SCHEME://$MUSIC_UI_BIND:$MUSIC_UI_PORT"
            tries=0
            until voice_control_ready || [ "$tries" -ge 10 ]; do
                sleep 0.1
                tries=$((tries + 1))
            done
            if ! voice_control_ready; then
                log "loopback voice-control endpoint did not respond on 127.0.0.1:$MUSIC_VOICE_CONTROL_PORT"
                stop_music_ui
                return 1
            fi
            log "loopback voice-control ready at 127.0.0.1:$MUSIC_VOICE_CONTROL_PORT"
            return 0
        fi
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
        i=$((i + 1))
    done
    log "music UI failed to start; see $LOG_DIR/music-ui.log"
    stop_music_ui
    return 1
}

ensure_music_discovery() {
    [ "$MUSIC_UI" = 1 ] && [ "$ROLE" = server-client ] || return 0
    if pid=$(find_pid "$MDNS_HELPER"); then
        return 0
    fi
    if [ ! -x "$MDNS_PYTHON" ] || [ ! -r "$MDNS_HELPER" ]; then
        log "LAN music discovery unavailable (need $MDNS_PYTHON and $MDNS_HELPER)"
        return 0
    fi
    setsid env "LD_LIBRARY_PATH=$MDNS_LIBRARY_PATH" "$MDNS_PYTHON" "$MDNS_HELPER" \
        >>"$LOG_DIR/mdns-ha.log" 2>&1 </dev/null &
    pid=$!
    i=0
    while [ "$i" -lt 20 ]; do
        if pid=$(find_pid "$MDNS_HELPER"); then
            log "LAN music discovery started PID $pid"
            return 0
        fi
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
        i=$((i + 1))
    done
    log "LAN music discovery did not start; see $LOG_DIR/mdns-ha.log"
    return 0
}

start_watchdog() {
    case "$ROLE" in client|server-client) ;; *) return 0 ;; esac
    if pid=$(find_pid "$0 supervise"); then
        echo "$pid" > "$WATCHDOG_PID_FILE"
        return 0
    fi
    setsid "$0" supervise >>"$LOG_DIR/watchdog.log" 2>&1 </dev/null &
    echo "$!" > "$WATCHDOG_PID_FILE"
}

reconcile_all() {
    # Discovery only selects a host when starting Snapclient. Repointing a live
    # client would interrupt music; use the explicit service restart for that.
    case "$ROLE" in
        server|server-client)
            if ! find_pid "$SERVER -c $SERVER_CONFIG" >/dev/null; then
                log "Snapserver is down; retrying"
                start_server || true
            fi
            ;;
    esac
    case "$ROLE" in
        client|server-client)
            # A live Snapclient can keep retrying an old server forever after
            # that host disappears. When discovery is enabled, probe the
            # cached host and switch only after a different advertised server
            # is reachable; preserve the current client while discovery fails.
            case "$SERVER_HOST" in
                auto|discover)
                    cached_host=
                    [ -r "$SERVER_CACHE_FILE" ] && cached_host=$(cat "$SERVER_CACHE_FILE" 2>/dev/null || true)
                    case "$cached_host" in ''|*[!0-9.]*) cached_host= ;; esac
                    if [ -n "$cached_host" ] && ! server_port_open "$cached_host"; then
                        now=$(date +%s 2>/dev/null || echo 0)
                        last=0
                        if [ -r "$DISCOVERY_RETRY_FILE" ]; then
                            last=$(cat "$DISCOVERY_RETRY_FILE" 2>/dev/null || echo 0)
                            case "$last" in ''|*[!0-9]*) last=0 ;; esac
                        fi
                        if [ "$now" -ge "$last" ] && [ $((now - last)) -ge 4 ]; then
                            printf '%s\n' "$now" > "$DISCOVERY_RETRY_FILE"
                            SERVER_HOST=auto
                            if resolve_server_host; then
                                if [ "$SERVER_HOST" != "$cached_host" ]; then
                                    log "music server changed $cached_host -> $SERVER_HOST; restarting client"
                                    stop_pidfile "$CLIENT_PID_FILE" "$CLIENT" Snapclient
                                    stop_pidfile "$BRIDGE_PID_FILE" "$BRIDGE" bridge
                                    # Discard any bytes still queued in the old
                                    # raw FIFO before the new stream attaches.
                                    [ ! -p "$RAW_FIFO" ] || rm -f "$RAW_FIFO"
                                    start_client || true
                                fi
                            else
                                SERVER_HOST=auto
                            fi
                        fi
                    fi
                    ;;
            esac
            client_pattern="$CLIENT tcp://$SERVER_HOST:$SERVER_PORT"
            case "$SERVER_HOST" in auto|discover) client_pattern="$CLIENT tcp://" ;; esac
            if ! find_pid "$client_pattern" >/dev/null \
                    || ! find_pid "$PYTHON $BRIDGE" >/dev/null; then
                # A frontend restart closes its FIFO reader; the file player may
                # then exit with EPIPE. Wait until the replacement frontend has
                # reopened the configured FIFO before recreating the client.
                if frontend_has_fifo; then
                    log "client/bridge is down; retrying"
                    start_client || true
                fi
            fi
            ;;
    esac
    if [ "$MUSIC_UI" = 1 ] && ! find_pid "$MUSIC_APP/server.py --bind $MUSIC_UI_BIND --port $MUSIC_UI_PORT" >/dev/null; then
        log "music UI is down; retrying"
        start_music_ui || true
    fi
    ensure_music_discovery || true
}

supervise() {
    trap 'exit 0' TERM INT
    while :; do
        sleep 2
        "$0" reconcile || true
    done
}

start_client() {
    [ -n "$SERVER_HOST" ] || { log "client role requires XR_SNAPCAST_SERVER_HOST"; return 1; }
    resolve_server_host || return 1
    [ -x "$CLIENT" ] || { log "Snapclient binary missing: $CLIENT"; return 1; }
    [ -x "$PYTHON" ] || { log "Python runtime missing: $PYTHON"; return 1; }
    [ -r "$BRIDGE" ] || { log "FIFO bridge missing: $BRIDGE"; return 1; }
    frontend_has_fifo || {
        log "frontend is not running with XR_MUSIC_FIFO=$MUSIC_FIFO; refusing to start"
        return 1
    }
    if [ -e "$RAW_FIFO" ] && [ ! -p "$RAW_FIFO" ]; then
        log "refusing to replace non-FIFO path: $RAW_FIFO"
        return 1
    fi
    if [ -e "$MUSIC_FIFO" ] && [ ! -p "$MUSIC_FIFO" ]; then
        log "refusing to replace non-FIFO path: $MUSIC_FIFO"
        return 1
    fi
    mkdir -p "$(dirname "$RAW_FIFO")" "$(dirname "$MUSIC_FIFO")"
    [ -p "$RAW_FIFO" ] || mkfifo "$RAW_FIFO"
    [ -p "$MUSIC_FIFO" ] || mkfifo "$MUSIC_FIFO"
    chmod 666 "$RAW_FIFO" "$MUSIC_FIFO"
    if pid=$(find_pid "$PYTHON $BRIDGE"); then
        echo "$pid" > "$BRIDGE_PID_FILE"
        log "bridge already running PID $pid"
    else
        # The bridge opens RAW_FIFO first, then waits for the frontend FIFO reader.
        setsid "$PYTHON" "$BRIDGE" "$RAW_FIFO" "$MUSIC_FIFO" mix \
            >>"$LOG_DIR/bridge.log" 2>&1 </dev/null &
        pid=$!
        echo "$pid" > "$BRIDGE_PID_FILE"
        sleep 0.2
        kill -0 "$pid" 2>/dev/null || { log "bridge failed to start"; return 1; }
    fi
    if pid=$(find_pid "$CLIENT tcp://$SERVER_HOST:$SERVER_PORT"); then
        echo "$pid" > "$CLIENT_PID_FILE"
        log "Snapclient already running PID $pid"
        return 0
    fi
    # Snapcast's Debian ARM64 build uses libraries bundled beside Snapserver.
    # Keep the client on the same private runtime path instead of installing
    # those dependencies into the system root.
    setsid env "LD_LIBRARY_PATH=$BASE/server-v0.35.0/lib:/usr/lib:/lib" \
        "$CLIENT" "tcp://$SERVER_HOST:$SERVER_PORT" \
        --player="file:filename=$RAW_FIFO" --mixer software \
        --logsink stdout --logfilter '*:warn' >>"$LOG_DIR/snapclient.log" 2>&1 </dev/null &
    pid=$!
    echo "$pid" > "$CLIENT_PID_FILE"
    sleep 0.3
    if ! kill -0 "$pid" 2>/dev/null; then
        log "Snapclient exited during startup; see $LOG_DIR/snapclient.log"
        rm -f "$CLIENT_PID_FILE"
        return 1
    fi
    log "Snapclient started PID $pid -> $SERVER_HOST:$SERVER_PORT"
}

start_all() {
    case "$ROLE" in
        server|server-client) start_server || return 1 ;;
    esac
    case "$ROLE" in
        client|server-client)
            if ! start_client; then
                stop_pidfile "$CLIENT_PID_FILE" "$CLIENT" Snapclient
                stop_pidfile "$BRIDGE_PID_FILE" "$BRIDGE" bridge
                case "$ROLE" in server-client) stop_pidfile "$SERVER_PID_FILE" "$SERVER" Snapserver ;; esac
                return 1
            fi
            ;;
    esac
    if ! start_music_ui; then
        stop_music_ui
        stop_pidfile "$CLIENT_PID_FILE" "$CLIENT" Snapclient
        stop_pidfile "$BRIDGE_PID_FILE" "$BRIDGE" bridge
        case "$ROLE" in server|server-client) stop_pidfile "$SERVER_PID_FILE" "$SERVER" Snapserver ;; esac
        return 1
    fi
    ensure_music_discovery
    start_watchdog
}

stop_all() {
    stop_pidfile "$WATCHDOG_PID_FILE" "$0 supervise" watchdog
    stop_music_ui
    stop_pidfile "$CLIENT_PID_FILE" "$CLIENT" Snapclient
    stop_pidfile "$BRIDGE_PID_FILE" "$BRIDGE" bridge
    case "$ROLE" in server|server-client) stop_pidfile "$SERVER_PID_FILE" "$SERVER" Snapserver ;; esac
    log "stopped"
}

status_all() {
    failed=0
    case "$ROLE" in
        server|server-client)
            if pid=$(find_pid "$SERVER -c $SERVER_CONFIG"); then log "Snapserver running PID $pid"; else log "Snapserver stopped"; failed=1; fi
            ;;
    esac
    case "$ROLE" in
        client|server-client)
            client_pattern="$CLIENT tcp://$SERVER_HOST:$SERVER_PORT"
            case "$SERVER_HOST" in auto|discover) client_pattern="$CLIENT tcp://" ;; esac
            if pid=$(find_pid "$client_pattern"); then log "Snapclient running PID $pid"; else log "Snapclient stopped"; failed=1; fi
            if pid=$(find_pid "$PYTHON $BRIDGE"); then log "bridge running PID $pid"; else log "bridge stopped"; failed=1; fi
            ;;
    esac
    if [ "$MUSIC_UI" = 1 ]; then
        if pid=$(find_pid "$MUSIC_APP/server.py --bind $MUSIC_UI_BIND --port $MUSIC_UI_PORT"); then log "music UI running PID $pid"; else log "music UI stopped"; failed=1; fi
    fi
    case "$ROLE" in
        client|server-client)
            if pid=$(find_pid "$0 supervise"); then log "watchdog running PID $pid"; else log "watchdog stopped"; failed=1; fi
            ;;
    esac
    return "$failed"
}

case "${1:-}" in
    supervise) supervise ;;
    reconcile) reconcile_all ;;
    resolve-server) resolve_server_host; echo "$SERVER_HOST" ;;
    start) start_all ;;
    stop) stop_all ;;
    restart) stop_all; sleep 1; start_all ;;
    status) status_all ;;
    *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
