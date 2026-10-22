#!/bin/sh
# Shared-code launcher. runtime.env contains paths/ports, never inline API keys.
set -eu
ROLE=${1:?usage: start-gateway-instance.sh ROLE}
case "$ROLE" in *[!a-zA-Z0-9_-]*|'') echo 'invalid role'; exit 2;; esac
ROOT=${XR_GATEWAY_ROOT:-/data/xiaorui-gateway}
PROFILE=$ROOT/instances/$ROLE
[ -f "$PROFILE/runtime.env" ] || { echo "missing profile: $ROLE"; exit 1; }
set -a
. "$PROFILE/runtime.env"
set +a
export XR_ROLE=$ROLE
PY=${XR_PYTHON:-/data/miniforge/bin/python}
: "${XR_CODE_DIR:?}" "${XR_WORK_DIR:?}" "${XR_DATA_DIR:?}" "${XR_CONFIG_FILE:?}" "${XR_HTTP_PORT:?}"
[ -f "$XR_CODE_DIR/app.py" ] && [ -f "$XR_CONFIG_FILE" ] || exit 1
mkdir -p "$PROFILE/run"
PIDFILE=$PROFILE/run/server.pid
LOG=$PROFILE/run/server.log
health() {
    "$PY" - "$XR_HTTP_PORT" "$ROLE" "${XR_RELEASE_ID:-legacy}" <<'PY'
import json,sys,urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:'+sys.argv[1]+'/health/ready', timeout=2) as r:
        d=json.load(r)
    sys.exit(0 if d.get('ready') and d.get('role')==sys.argv[2] and d.get('release')==sys.argv[3] else 1)
except Exception:
    sys.exit(1)
PY
}
# 健康快检必须排在锁前: 服务端进程会终身持有锁 fd, 先抢锁会让 watch
# 在健康实例上每拍都报 already starting 退出 1, 真假故障无法区分.
if health; then exit 0; fi
if [ "${XR_START_LOCKED:-0}" != 1 ]; then
    # Kernel lock releases on SIGKILL/reboot: no stale mkdir lock blocks boot.
    # Bounded wait: SIGTERM shutdown can take seconds; an immediate restart must
    # wait out the dying holder instead of failing "already starting".
    exec "$PY" - "$PROFILE/run/start.lock" "$0" "$ROLE" <<'PY'
import fcntl,os,subprocess,sys,time
with open(sys.argv[1], 'a') as lock:
    deadline = time.monotonic() + 20
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                print('instance is already starting')
                sys.exit(1)
            time.sleep(.5)
    env=dict(os.environ, XR_START_LOCKED='1')
    sys.exit(subprocess.run(['sh',sys.argv[2],sys.argv[3]],env=env,
                            pass_fds=(lock.fileno(),)).returncode)
PY
fi
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    case "$PID" in *[!0-9]*|'') exit 1;; esac
    if kill -0 "$PID" 2>/dev/null &&
       tr '\000' '\n' < "/proc/$PID/cmdline" | grep -q '/app[.]py$'; then
        echo "instance $ROLE exists but is not ready; inspect $LOG"; exit 1
    fi
fi
cd "$XR_WORK_DIR"
# provider 探测(llm/asr/tts/intent)与模型/曲库均按相对路径解析(cwd 锚定):
# core 软链让共享代码可见, models/music 软链共享只读资源, tmp 与日志落在
# 各自 work 下, 两实例互不串写.
ln -sfn "$XR_CODE_DIR/core" "$XR_WORK_DIR/core"
[ -e "$XR_WORK_DIR/models" ] || ln -sfn /data/xiaorui-server/server/models "$XR_WORK_DIR/models"
[ -e "$XR_WORK_DIR/music" ] || ln -sfn /data/xiaorui-gateway/server/music "$XR_WORK_DIR/music"
[ -e "$XR_WORK_DIR/music-online" ] || ln -sfn /data/xiaorui-gateway/server/music-online "$XR_WORK_DIR/music-online"
mkdir -p "$XR_WORK_DIR/tmp"
(setsid "$PY" "$XR_CODE_DIR/app.py" > "$LOG" 2>&1 < /dev/null & echo $! > "$PIDFILE")
i=0
while [ "$i" -lt 60 ]; do
    if health; then echo "instance $ROLE ready release=${XR_RELEASE_ID:-legacy}"; exit 0; fi
    i=$((i+1)); sleep 1
done
echo "instance $ROLE readiness failed; inspect $LOG"
exit 1
