#!/bin/bash
# 板端联网网关验收编排 (PC 侧, adb + PC 喇叭声学闭环).
# 用法: run-board-acceptance.sh [-s serial] [-o outdir] [-d soak分钟] [--stages a,b,c]
#   阶段: snapshot dialog fault soak reboot   (reboot 含晋升, 仅在前序全绿时执行)
#
# 实测教训(2026-10-14, 已固化到脚本):
#   - 默认用纯唤醒+3秒间隔+报时指令，避免旧pos049晚安尾词漂移和结束对话意图。
#     独立指令测试用"冷唤醒+报时→等回答完毕(窗口续开)→窗内跟说"两段式.
#   - qwen-flash 长回答 4-6 句, 板上 Matcha 逐句合成, 单轮可达 60-90s → 等待 150s.
#   - 回答尾音会被麦收到(半双工自听), 每轮后等日志静默 12s 再放下一条.
#   - 板上无 iptables → 断云用 ip route blackhole default (adb 走 USB 不受影响).
#   - pgrep -x 被 15 字符 comm 截断坑 → 一律 /proc/*/comm 或 pgrep -f 精确路径.
#   - adb 内联 setsid 会挂住调用方 → 板上启动器脚本模式.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERIAL="${XZ_BOARD_SERIAL:-}"
SOAK_MIN=30
STAGES="snapshot,dialog,fault,soak,reboot"
TS=$(date +%Y%m%d-%H%M%S)
OUT="${XR_ACCEPT_OUT:-$REPO/test-results/accept-$TS}"
AUDIO="$REPO/tests/audio"
PLAY_DEV="${PLAY_DEV:-plughw:1,0}"  # 明确设备, 不走会漂移的默认 sink; 现场可覆盖
WAKE1=wake2_time.wav
ADB="adb ${SERIAL:+-s $SERIAL}"
xr_log=/tmp/xr_live.log
PROD_SHA=""
CAND_SHA=""
EXPECTED_SHA=""
WAIT_HAS_EVIDENCE=0
WAIT_FAILURE=""
SELFCHECK_ATTEMPTS=0

while [ $# -gt 0 ]; do
    case "$1" in
        -s) SERIAL="$2"; ADB="adb ${SERIAL:+-s $SERIAL}"; shift 2 ;;
        -o) OUT="$2"; shift 2 ;;
        -d) SOAK_MIN="$2"; shift 2 ;;
        --stages) STAGES="$2"; shift 2 ;;
        *) echo "未知参数 $1"; exit 2 ;;
    esac
done
mkdir -p "$OUT"
echo "[accept] outdir=$OUT stages=$STAGES"

note() { echo "$*" | tee -a "$OUT/summary.txt"; }
board() {
    local limit=${BOARD_TIMEOUT:-20} remaining
    if [ -n "${SELFCHECK_DEADLINE:-}" ]; then
        remaining=$((SELFCHECK_DEADLINE-SECONDS))
        [ "$remaining" -gt 0 ] || return 124
        [ "$remaining" -ge "$limit" ] || limit=$remaining
    fi
    timeout "$limit" $ADB shell "$@"
}
board_stdin() { timeout 20 $ADB shell "$@"; }
ROUTE_ADDED=0
CRON_SAVED=0
CRON_ORIGINAL=""
PROMOTION_PENDING=0
PRODUCTION_BACKUP=""
MON=""
FRONTEND_TOUCHED=0

restore_frontend() {
    [ "$FRONTEND_TOUCHED" -eq 1 ] || return 0
    local BOARD_TIMEOUT=65
    board 'sh /data/xiaorui-gateway/restore-frontend.sh' || return 1
    FRONTEND_TOUCHED=0
}

add_blackhole() {
    board "ip route add blackhole default" || return 1
    ROUTE_ADDED=1
}
remove_blackhole() {
    [ "$ROUTE_ADDED" -eq 1 ] || return 0
    board "ip route del blackhole default" || return 1
    ROUTE_ADDED=0
}
rollback_promotion() {
    [ "$PROMOTION_PENDING" -eq 1 ] || return 0
    # Rename over a running executable; in-place cp can fail with ETXTBSY.
    board "cp '$PRODUCTION_BACKUP' /data/xiaorui-server/board-scripts/xiaorui_live.restore-$TS && chmod 755 /data/xiaorui-server/board-scripts/xiaorui_live.restore-$TS && mv -f /data/xiaorui-server/board-scripts/xiaorui_live.restore-$TS /data/xiaorui-server/board-scripts/xiaorui_live" || return 1
    board "pkill -x xiaorui_live; sh /data/xiaorui-server/start-all.sh" || return 1
    PROMOTION_PENDING=0
    note "reboot: 已恢复晋升前生产二进制"
}
cleanup() {
    local rc=$?
    trap - EXIT
    [ -z "$MON" ] || kill "$MON" 2>/dev/null || true
    remove_blackhole || { echo "ERROR: 黑洞路由恢复失败, 需人工恢复" >&2; rc=1; }
    cron_on || { echo "ERROR: cron 恢复失败, 需人工恢复" >&2; rc=1; }
    rollback_promotion || { echo "ERROR: 生产回滚失败, 备份=$PRODUCTION_BACKUP" >&2; rc=1; }
    restore_frontend || { echo 'ERROR: frontend restoration failed' >&2; rc=1; }
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
wait_device() { timeout 360 $ADB wait-for-device; }
cloud_answer_since() {
    local segment
    segment=$(board "tail -c +$(($1+1)) $xr_log") || return 1
    grep -q 'audio submitted' <<< "$segment" &&
        grep -q '回答完毕' <<< "$segment" &&
        ! grep -q '转本地\|切本地\|本地 ASR 兜底\|合成 stop' <<< "$segment"
}
logpos() { board "wc -c <$xr_log 2>/dev/null" | tr -d '\r\n '; }

require_candidate() {
    local hashes
    EXPECTED_SHA=$(sha256sum "$REPO/board/xiaorui_live" | awk '{print $1}') || return 1
    hashes=$(board "sha256sum /data/xiaorui-server/board-scripts/xiaorui_live /data/xiaorui-gateway/xiaorui_live-cand") || return 1
    PROD_SHA=$(awk 'NR==1 {print $1}' <<< "$hashes")
    CAND_SHA=$(awk 'NR==2 {print $1}' <<< "$hashes")
    note "prod_sha=$PROD_SHA candidate_sha=$CAND_SHA expected_sha=$EXPECTED_SHA"
    if ! [[ "$PROD_SHA" =~ ^[0-9a-f]{64}$ && "$CAND_SHA" =~ ^[0-9a-f]{64}$ ]]; then
        note "candidate-check=FAIL (缺失或非法 SHA)"; return 1
    fi
    if [ "$CAND_SHA" = "$PROD_SHA" ]; then
        note "candidate-check=FAIL (候选与生产相同, 未部署本轮候选)"; return 1
    fi
    if [ "$CAND_SHA" != "$EXPECTED_SHA" ]; then
        note "candidate-check=FAIL (板上候选与本机构建不一致)"; return 1
    fi
}

wait_turn() { # <expected-wav> <timeout> <offset>; preserves observed texts for scoring
    local wav="$1" tmo="$2" off="$3" i=0 rc=1
    local deadline=$((SECONDS+tmo))
    if [ -n "${SELFCHECK_DEADLINE:-}" ] && [ "$deadline" -gt "$SELFCHECK_DEADLINE" ]; then
        deadline=$SELFCHECK_DEADLINE
    fi
    WAIT_HAS_EVIDENCE=0
    WAIT_FAILURE=""
    while [ "$SECONDS" -lt "$deadline" ]; do
        if ! board "tail -c +$((off+1)) $xr_log" > "$OUT/turn-segment.log"; then
            WAIT_FAILURE=log-unavailable; return 3
        fi
        python3 "$REPO/tools/score-acceptance-turn.py" --manifest "$AUDIO/manifest.json" \
            --wav "$wav" < "$OUT/turn-segment.log" > "$OUT/last-turn.json"
        rc=$?
        [ ! -s "$OUT/last-turn.json" ] || WAIT_HAS_EVIDENCE=1
        [ "$rc" -ne 0 ] || return 0
        if [ "$rc" -ne 1 ]; then WAIT_FAILURE=transcript-or-turn-rejected; return 2; fi
        sleep 1; i=$((i+1))
    done
    WAIT_FAILURE=timeout-incomplete-turn
    if ! grep -qE '检测到说话|说完|指令:|ASR 回执:|跟说音频' "$OUT/turn-segment.log" 2>/dev/null; then
        WAIT_FAILURE=no-mic-activity
    fi
    return 1
}
record_turn() { # <id> <category> <wav> <phase> [failure]
    local evidence=()
    [ "$WAIT_HAS_EVIDENCE" -ne 1 ] || evidence=(--evidence "$OUT/last-turn.json")
    python3 "$REPO/tools/score-acceptance-turn.py" --manifest "$AUDIO/manifest.json" \
        --id "$1" --cat "$2" --wav "$3" --phase "$4" --failure "${5:-}" \
        "${evidence[@]}" < /dev/null >> "${TURN_RECORD_FILE:-$OUT/turns.jsonl}" || true
}

fixture_selfcheck() {
    # Fail fast before soak/reboot/fault injection. Never learn variants from
    # the same run being graded, which would make echo transcripts pass.
    local SELFCHECK_DEADLINE=$((SECONDS+120))
    local TURN_RECORD_FILE="$OUT/selfcheck.jsonl" off attempt rc
    restart_frontend || { note "fixture-selfcheck=FAIL (候选启动失败)"; return 1; }
    note "fixture-selfcheck play_dev=$PLAY_DEV settle=8s attempts≤2 (仅无麦活动超时可重试)"
    sleep 8
    for attempt in 1 2; do
        SELFCHECK_ATTEMPTS=$attempt
        off=$(logpos)
        WAIT_HAS_EVIDENCE=0
        play "$WAKE1" || { note "fixture-selfcheck=FAIL (音频播放失败, device=$PLAY_DEV)"; return 1; }
        if wait_turn "$WAKE1" 35 "$off"; then
            record_turn "selfcheck-$attempt" fixture "$WAKE1" command
            note "fixture-selfcheck=PASS attempts=$attempt readiness_retries=$((attempt-1))"
            settle 12 15
            return 0
        else
            rc=$?
        fi
        record_turn "selfcheck-$attempt" fixture "$WAKE1" command "${WAIT_FAILURE:-selfcheck-failed}"
        if [ "$attempt" -ne 1 ] || [ "$rc" -ne 1 ] || [ "$WAIT_FAILURE" != "no-mic-activity" ]; then break; fi
        note "fixture-selfcheck retry=1 reason=no-mic-activity (不计入dialog/soak成功轮次)"
        sleep 8
    done
    note "fixture-selfcheck=FAIL reason=${WAIT_FAILURE:-unknown} (未进入长跑/故障/晋升)"
    return 1
}

wait_xr() { # wait_xr <pattern> <timeout_s> [from_offset]
    local pat="$1" tmo="${2:-30}" off="${3:-0}" i=0 p rc
    if [ "$pat" = "回答完毕" ]; then wait_turn "$WAKE1" "$tmo" "$off"; return $?; fi
    local deadline=$((SECONDS+tmo))
    if [ -n "${SELFCHECK_DEADLINE:-}" ] && [ "$deadline" -gt "$SELFCHECK_DEADLINE" ]; then
        deadline=$SELFCHECK_DEADLINE
    fi
    while [ "$SECONDS" -lt "$deadline" ]; do
        p=$(logpos)
        if [ "$p" -gt "$off" ] 2>/dev/null; then
            if board "tail -c +$((off+1)) $xr_log 2>/dev/null | grep -q \"$pat\""; then
                return 0
            fi
        fi
        [ -n "${OUT:-}" ] && echo "wait_xr i=$i off=$off pos=$p pat=$pat" >> "$OUT/wait-dbg.log"
        sleep 1; i=$((i+1))
    done
    return 1
}
settle() { # 等日志静默, 但有绝对时限 -- 自听轮会不断续窗, 静默条件可能永不满足
    local quiet="${1:-12}" cap="${2:-90}" p1 p2 e=0
    p1=$(logpos)
    while [ "$e" -lt "$cap" ]; do
        if [ -n "${SELFCHECK_DEADLINE:-}" ] && [ "$SECONDS" -ge "$SELFCHECK_DEADLINE" ]; then return 0; fi
        sleep 3; e=$((e+3))
        p2=$(logpos)
        if [ "$p2" = "$p1" ]; then quiet=$((quiet-3)); else quiet="${1:-12}"; fi
        p1=$p2
        [ "$quiet" -le 0 ] && return 0
    done
    return 0   # 超时也放行(自听轮的窗口 30s 后自然过期)
}
play() {
    if [ -n "${SELFCHECK_DEADLINE:-}" ]; then
        local remaining=$((SELFCHECK_DEADLINE-SECONDS))
        [ "$remaining" -gt 0 ] || return 124
        timeout "$remaining" aplay -D "$PLAY_DEV" -q "$AUDIO/$1"
    else
        aplay -D "$PLAY_DEV" -q "$AUDIO/$1"
    fi
}
set_vol() { amixer -q sset Master 100% 2>/dev/null; amixer -q sset Speaker 100% 2>/dev/null; true; }

restart_frontend() {
    timeout 20 $ADB push "$REPO/board-server/start-frontend-cand.sh" /data/xiaorui-gateway/start-frontend-cand.sh >/dev/null || return 1
    timeout 20 $ADB push "$REPO/board-server/restore-frontend.sh" /data/xiaorui-gateway/restore-frontend.sh >/dev/null || return 1
    FRONTEND_TOUCHED=1
    local BOARD_TIMEOUT=60
    board "sh /data/xiaorui-gateway/start-frontend-cand.sh" || return 1
    wait_xr "云端在线, 云端优先" 25 || wait_xr "纯本地" 25
}
round_one_phase() { # <id> <category>; one attempt, no scored-round retries
    local id="$1" cat="$2" off
    WAIT_HAS_EVIDENCE=0
    off=$(logpos)
    play "$WAKE1" || { record_turn "$id" "$cat" "$WAKE1" command play-fail; return 1; }
    if wait_turn "$WAKE1" 150 "$off"; then
        record_turn "$id" "$cat" "$WAKE1" command
        settle 12; return 0
    fi
    record_turn "$id" "$cat" "$WAKE1" command command-or-answer-failed
    return 1
}
round_two_phase() { # <id> <category> <cmd-wav>  冷唤醒→回答完(窗口续开)→窗内跟说
    local id="$1" cat="$2" cmd="$3" off1 off2
    WAIT_HAS_EVIDENCE=0
    off1=$(logpos)
    play "$WAKE1" || { record_turn "$id" "$cat" "$WAKE1" wake play-fail; return 1; }
    wait_turn "$WAKE1" 150 "$off1" || { record_turn "$id" "$cat" "$WAKE1" wake wake-phase-failed; return 1; }
    settle 12
    off2=$(logpos)
    WAIT_HAS_EVIDENCE=0
    play "$cmd" || { record_turn "$id" "$cat" "$cmd" command cmd-play-fail; return 1; }
    if wait_turn "$cmd" 300 "$off2"; then
        record_turn "$id" "$cat" "$cmd" command
        settle 12; return 0
    fi
    record_turn "$id" "$cat" "$cmd" command command-or-answer-failed
    return 1
}

# ---------------- snapshot ----------------
stage_snapshot() {
    require_candidate || return 1
    { echo "== snapshot $(date -u +%FT%TZ) =="
      $ADB get-state
      board "cat /proc/uptime; ss -tln | grep -E ':8000 |:8001 |:8003 |:8004 |:8112 '"
      board "sha256sum /data/xiaorui-server/board-scripts/xiaorui_live /data/xiaorui-gateway/xiaorui_live-cand | sed 's/\(.\{16\}\)[0-9a-f]*/\1…/'"
      board "sh /data/xiaorui-gateway/check-network.sh"
      echo "== PC 旧链下线(证明业务不经 PC/ADB 隧道) =="
      adb reverse --list
      adb reverse --remove tcp:9100 2>/dev/null && echo "adb reverse tcp:9100 已移除" || echo "无 9100 隧道"
      systemctl --user stop pc-gateway 2>/dev/null && echo "pc-gateway 已停(新架构不需要)" || echo "pc-gateway 未运行"
      systemctl --user stop pc-xiaozhi 2>/dev/null && echo "pc-xiaozhi 已停(新架构不需要)" || echo "pc-xiaozhi 未运行"
      set_vol
    } > "$OUT/snapshot.log" 2>&1
    grep -qE "RESULT pass=[0-9]+ fail=0" "$OUT/snapshot.log"
}

# 热身轮(不计分): ①先等 40s 让残留跟说窗(30s)与回声护栏(4s)过期, 掐断
# "应答音→自听→又续窗"的自供能循环(实测该循环会淹没 PC 放的唤醒词);
# ②再放唤醒并要求看到"检测到说话"证据, 最多重试 3 次.
warmup() {
    local off try
    settle 12 40   # 掐自听循环: 上限 40s > 30s 跟说窗
    for try in 1 2 3; do
        off=$(logpos)
        play "$WAKE1" 2>/dev/null
        if wait_xr "回答完毕" 60 "$off"; then
            settle 12 60
            note "warmup: 第 $try 次通过"
            return 0
        fi
        sleep 15
    done
    note "warmup: FAIL 3 次均无完成轮次"
    return 1
}

# ---------------- dialog ----------------
stage_dialog() {
    restart_frontend || return 1
    warmup || return 1
    : > "$OUT/turns.jsonl"
    round_one_phase D1 cold-wake
    round_two_phase D2 in-window-cmd cmd_time.wav
    round_two_phase D3 in-window-cmd cmd_weather.wav
    round_two_phase D4 short-2char short_stop.wav
    round_two_phase D5 long-answer long_story.wav
    round_two_phase D6 long-answer long_history.wav
    sleep 32   # 等 30s 免唤醒窗过期
    round_one_phase D7 cold-wake-again
    local ok cloud long short counts
    counts=$(python3 - "$OUT/turns.jsonl" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if len(rows) != 7 or {row["id"] for row in rows} != {f"D{i}" for i in range(1, 8)}:
        raise ValueError("dialog requires seven unique round IDs")
    passed = [row for row in rows if row.get("ok") is True and row.get("phase") == "command"]
    print(len(passed), sum(row.get("cat") == "long-answer" for row in passed),
          sum(row.get("cat") == "short-2char" for row in passed))
except (OSError, ValueError, KeyError, TypeError) as error:
    print(f"invalid dialog evidence: {error}", file=sys.stderr)
    sys.exit(1)
PY
) || { note 'dialog: FAIL invalid JSONL evidence'; return 1; }
    read -r ok long short <<< "$counts"
    cloud=$(board "grep -c 'audio submitted' $xr_log")
    note "dialog: $ok/7 轮完成, retries=0 cloud_audio=$cloud, long=$long, short=$short (要求: 7轮+≥2长+1短)"
    [ "$ok" -eq 7 ] && [ "$long" -ge 2 ] && [ "$short" -ge 1 ] && [ "$cloud" -ge 3 ]
}

# ---------------- fault ----------------
# crond 看护临时摘除/恢复(故障注入期间防止 */5 拉活网关干扰判定; 与部署器同款条目)
cron_off() {
    CRON_ORIGINAL=$(board "crontab -l 2>/dev/null || true") || return 1
    CRON_SAVED=1
    board "(crontab -l 2>/dev/null | grep -v 'xiaozhi-gateway/start-gateway.sh') | crontab -"
}
cron_on() {
    [ "$CRON_SAVED" -eq 1 ] || return 0
    printf '%s\n' "$CRON_ORIGINAL" | board_stdin 'crontab -' || return 1
    CRON_SAVED=0
}

stage_fault() {
    local rc=0
    warmup || return 1   # 麦克风听觉证据(环境病: ZR 卡中途失聪/门限爬升)
    # F1 空闲期杀网关 → 本地兜底 (crond 看护先摘掉, 否则 */5 拉活会干扰判定)
    cron_off || return 1
    local off=$(logpos) k
    for k in 1 2 3; do
        board 'PID=$(pgrep -f "[/]data/xiaorui-gateway/releases/[^/]*/server/app[.]py"); [ -n "$PID" ] && kill $PID; sleep 3; P=$(pgrep -f "[/]data/xiaorui-gateway/releases/[^/]*/server/app[.]py"); [ -n "$P" ] && kill -9 $P; sleep 1' >> "$OUT/f1.log" 2>&1
        board "ss -tln | grep -q ':8001 '" >> "$OUT/f1.log" 2>&1 || break
    done
    if board "ss -tln | grep -q ':8001 '" 2>/dev/null; then
        echo "F1 SKIP: 网关 8001 三次杀不透(环境异常, 本地兜底由 run6 F1+主机门禁证明)" >> "$OUT/f1.log"
        cron_on
        return 1
    fi
    off=$(logpos)
    play "$WAKE1"
    if wait_xr "回答完毕" 150 "$off" && board "tail -c +$((off+1)) $xr_log" | grep -qE "本轮转本地|云端探测失败|\[be\] turn="; then
        echo "F1 local-fallback PASS" >> "$OUT/f1.log"
    else
        echo "F1 local-fallback FAIL" >> "$OUT/f1.log"; rc=1
    fi
    settle 12
    # F2 拉回网关 → ≤3 轮内回云(音频或文本)
    board "sh /data/xiaorui-gateway/start-gateway.sh" >> "$OUT/f2.log" 2>&1
    local back=0 off2 i
    for i in 1 2 3; do
        off2=$(logpos)
        play "$WAKE1"
        wait_xr "回答完毕" 150 "$off2" || break
        if cloud_answer_since "$off2"; then back=1; break; fi
        settle 12
    done
    echo "F2 cloud-recover back=$back" >> "$OUT/f2.log"
    [ "$back" = "1" ] || rc=1
    # F3 回答中途杀网关 → 单次收尾不重复补答
    off=$(logpos)
    play "$WAKE1"
    wait_xr "sentence_start" 60 "$off" || { echo "F3 no-start" >> "$OUT/f3.log"; rc=1; }
    sleep 1.5
    board 'PID=$(pgrep -f "[/]data/xiaorui-gateway/releases/[^/]*/server/app[.]py"); [ -n "$PID" ] && kill $PID' >> "$OUT/f3.log" 2>&1
    if wait_xr "回答完毕" 120 "$off"; then
        local dones; dones=$(board "tail -c +$((off+1)) $xr_log" | grep -c "回答完毕")
        [ "$dones" -eq 1 ] && echo "F3 midanswer-kill PASS(单次收尾)" >> "$OUT/f3.log" \
            || { echo "F3 midanswer-kill FAIL(回答完毕×$dones)" >> "$OUT/f3.log"; rc=1; }
    else
        echo "F3 midanswer-kill FAIL(无收尾)" >> "$OUT/f3.log"; rc=1
    fi
    # 等旧进程死透再拉起(实测: 未死透时启动器防双开会拒绝拉新, 网关 15s 不监听)
    local gw_up=0 k
    for k in 1 2 3; do
        board 'L=$(pgrep -f "[/]data/xiaorui-gateway/releases/[^/]*/server/app[.]py"); [ -n "$L" ] && kill -9 $L; sleep 1' >> "$OUT/f3.log" 2>&1
        sleep 2
        if board "sh /data/xiaorui-gateway/start-gateway.sh" >> "$OUT/f3.log" 2>&1; then gw_up=1; break; fi
        sleep 5
    done
    [ "$gw_up" = "1" ] || { echo "F3 gateway-restart FAIL" >> "$OUT/f3.log"; rc=1; }
    settle 12
    # F4 断云不断板: blackhole 默认路由(adb/USB 不受影响) → 本地应答; 解除 → 回云
    # (前置: 网关必须监听, 不把 F3 的残留状态带进来)
    board "ss -tln | grep -q ':8001 ' || sh /data/xiaorui-gateway/start-gateway.sh" > "$OUT/f4.log" 2>&1
    add_blackhole >> "$OUT/f4.log" 2>&1
    if [ $? -eq 0 ]; then
        sleep 65   # 过 60s 探测窗, 让云端腿彻底判死
        off=$(logpos)
        play "$WAKE1"
        wait_xr "回答完毕" 150 "$off" && echo "F4 blocked-egress answered" >> "$OUT/f4.log" \
            || { echo "F4 FAIL no-answer" >> "$OUT/f4.log"; rc=1; }
        remove_blackhole >> "$OUT/f4.log" 2>&1 || return 1
        sleep 65
        back=0
        for i in 1 2 3; do
            off2=$(logpos)
            play "$WAKE1"
            wait_xr "回答完毕" 150 "$off2" || break
            cloud_answer_since "$off2" && { back=1; break; }
            settle 12
        done
        echo "F4 egress-recover back=$back" >> "$OUT/f4.log"
        [ "$back" = "1" ] || rc=1
    else
        echo "F4 SKIP: 无法加 blackhole 路由" >> "$OUT/f4.log"
        rc=1
    fi
    cron_on   # 恢复 crond 看护
    return $rc
}

# ---------------- soak ----------------
stage_soak() {
    # 环境复位: 整机重启(USB 声卡长跑失聪的对症手段) + 候选前端 + 热身
    $ADB reboot
    wait_device || return 1
    local w=0
    sleep 20
    w=0
    while [ $w -lt 40 ] && ! board "ss -tln | grep -q ':8001 '" 2>/dev/null; do sleep 3; w=$((w+1)); done
    restart_frontend || return 1
    warmup || return 1
    local total=$((SOAK_MIN * 60)) start=$(date +%s)
    ( while [ $(( $(date +%s) - start )) -lt $((total + 120)) ]; do
        $ADB shell "date -u +%FT%TZ; for p in \$(pgrep -f '[x]iaorui_live|[x]iaozhi-gateway/server/app[.]py|[x]iaozhi-server/server/app[.]py|[l]lm_rkllm[.]py'); do echo \"pid=\$p rss=\$(awk '/VmRSS/{print \$2}' /proc/\$p/status 2>/dev/null) fd=\$(ls /proc/\$p/fd 2>/dev/null | wc -l) th=\$(awk '/Threads/{print \$2}' /proc/\$p/status 2>/dev/null)\"; done; cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null" \
            >> "$OUT/monitor.csv" 2>&1
        sleep 60
      done ) &
    MON=$!
    : > "$OUT/soak-turns.jsonl"
    local shorts=(cmd_time cmd_weather cmd_alarm cmd_joke cmd_name cmd_music cmd_thanks cmd_mute cmd_repeat cmd_volume short_stop)
    local longs=(long_story long_history long_cook long_sci long_travel)
    local plan=() n=0
    while [ $n -lt 25 ]; do
        plan+=("S:${shorts[$((n % 11))]}")
        n=$((n+1))
    done
    for l in "${longs[@]}"; do plan+=("L:$l"); done   # 30 轮均冷唤醒后跟说真实指令
    local answered=0 long_ok=0 idx=0 item kind wav off target
    for item in "${plan[@]}"; do
        idx=$((idx+1))
        kind="${item%%:*}"; wav="${item#*:}"
        target=$(( start + idx * total / ${#plan[@]} ))
        while [ $(date +%s) -lt $target ]; do sleep 5; done
        [ $(( $(date +%s) - start )) -gt $((total + 300)) ] && break
        off=$(logpos)
        if round_two_phase "soak-$idx" "$kind" "$wav.wav"; then
            answered=$((answered+1))
            [ "$kind" != "L" ] || long_ok=$((long_ok+1))
            echo "{\"id\":$idx,\"kind\":\"$kind\",\"wav\":\"$wav.wav\",\"ok\":true}" >> "$OUT/soak-turns.jsonl"
        else
            echo "{\"id\":$idx,\"kind\":\"$kind\",\"wav\":\"$wav.wav\",\"ok\":false}" >> "$OUT/soak-turns.jsonl"
        fi
    done
    kill "$MON" 2>/dev/null || true
    MON=""
    note "soak: answered=$answered/30 long_ok=$long_ok (要求30/30 且5/5, 不重试掩盖失败)"
    [ "$answered" -eq 30 ] && [ "$long_ok" -eq 5 ]
}

# ---------------- reboot (含晋升门控) ----------------
declare -A RES
stage_reboot_impl() {
    # 必须是同一次运行的完整前置验收, 缺失/失败都不能晋升.
    local st
    for st in snapshot dialog fault soak; do
        if [ "${RES[$st]:-MISSING}" != "PASS" ]; then
            note "reboot: SKIP (前置 $st=${RES[$st]:-MISSING}, 不晋升)"
            return 1
        fi
    done
    PRODUCTION_BACKUP="/data/xiaorui-server/board-scripts/xiaorui_live.bak-accept-$TS-$$"
    board "cp /data/xiaorui-server/board-scripts/xiaorui_live '$PRODUCTION_BACKUP'" > "$OUT/promote.log" 2>&1 || return 1
    PROMOTION_PENDING=1
    board "cp /data/xiaorui-gateway/xiaorui_live-cand /data/xiaorui-server/board-scripts/xiaorui_live.promote-$TS && chmod 755 /data/xiaorui-server/board-scripts/xiaorui_live.promote-$TS && mv -f /data/xiaorui-server/board-scripts/xiaorui_live.promote-$TS /data/xiaorui-server/board-scripts/xiaorui_live" >> "$OUT/promote.log" 2>&1 \
        || { echo "晋升失败" >> "$OUT/promote.log"; return 1; }
    echo "候选已晋升生产: $(board "sha256sum /data/xiaorui-server/board-scripts/xiaorui_live" | cut -c1-16)" >> "$OUT/promote.log"
    $ADB reboot
    wait_device || return 1
    local i=0
    sleep 20
    local j=0
    while [ $j -lt 60 ] && ! board "ss -tln | grep -q ':8001 '" 2>/dev/null; do sleep 3; j=$((j+1)); done
    { echo "== reboot $(date -u +%FT%TZ) =="
      board "ss -tln | grep -E ':8000 |:8001 |:8112 '"
      board "pgrep -x xiaorui_live >/dev/null && echo frontend-up || echo frontend-missing"
      board "grep -c '云端在线' $xr_log"
    } > "$OUT/reboot.log" 2>&1
    set_vol
    warmup >> "$OUT/reboot.log" 2>&1 || { echo "R0 warmup FAIL" >> "$OUT/reboot.log"; return 1; }
    local off=0; off=$(logpos)
    play "$WAKE1"
    wait_xr "回答完毕" 150 "$off" && echo "R1 after-reboot PASS" >> "$OUT/reboot.log" || echo "R1 after-reboot FAIL" >> "$OUT/reboot.log"
    settle 12
    # 无网模拟: blackhole 默认路由 → 本地应答; 解除 → 回云
    board "ip route | grep '^default'" >> "$OUT/reboot.log" 2>&1
    add_blackhole >> "$OUT/reboot.log" 2>&1 || return 1
    sleep 65
    off=$(logpos)
    play "$WAKE1"
    wait_xr "回答完毕" 150 "$off" && echo "R2 no-net local PASS" >> "$OUT/reboot.log" || echo "R2 no-net FAIL" >> "$OUT/reboot.log"
    remove_blackhole >> "$OUT/reboot.log" 2>&1 || return 1
    sleep 65
    local back=0 off2
    for i in 1 2 3; do
        off2=$(logpos)
        play "$WAKE1"
        wait_xr "回答完毕" 150 "$off2" || break
        cloud_answer_since "$off2" && { back=1; break; }
        settle 12
    done
    echo "R3 net-recover back=$back" >> "$OUT/reboot.log"
    if grep -q "R1 after-reboot PASS" "$OUT/reboot.log" && grep -q "R2 no-net local PASS" "$OUT/reboot.log" && [ "$back" = "1" ]; then
        PROMOTION_PENDING=0
        return 0
    fi
    rollback_promotion
    return 1
}

stage_reboot() {
    local rc=0
    stage_reboot_impl || rc=1
    if [ "$rc" -ne 0 ]; then
        remove_blackhole || return 1
        rollback_promotion || return 1
    fi
    return "$rc"
}

# Host tests source functions with mocked ADB; never execute production stages.
if [ "${XR_ACCEPT_LIBRARY_ONLY:-0}" = "1" ]; then return 0; fi
exec 9>"/tmp/xiaozhi-vqe-accept-$SERIAL.lock"
flock -n 9 || { note "preflight=FAIL another acceptance run owns this board"; exit 1; }
require_candidate || { note "preflight=FAIL fails=1 (未执行任何验收阶段)"; exit 1; }
note "scoring_policy=实测变体+fixture受限正则; 其余编辑距离: ≤2字=0, 3-4字≤1, ≥5字≤2; play_dev=$PLAY_DEV"
if [ "$STAGES" != "snapshot" ]; then
    fixture_selfcheck || { note "preflight=FAIL fails=1 (fixture自检失败)"; exit 1; }
fi

# ---------------- 汇总 ----------------
for st in ${STAGES//,/ }; do
    case $st in
        snapshot) stage_snapshot && RES[$st]=PASS || RES[$st]=FAIL ;;
        dialog)   stage_dialog && RES[$st]=PASS || RES[$st]=FAIL ;;
        fault)    stage_fault && RES[$st]=PASS || RES[$st]=FAIL ;;
        soak)     stage_soak && RES[$st]=PASS || RES[$st]=FAIL ;;
        reboot)   stage_reboot && RES[$st]=PASS || RES[$st]=FAIL ;;
    esac
    echo "[accept] $st=${RES[$st]}"
done
fails=0
for st in ${STAGES//,/ }; do [ "${RES[$st]}" = "PASS" ] || fails=$((fails+1)); done
{
  echo "serial=$SERIAL"
  echo "utc=$(date -u +%FT%TZ)"
  echo "initial_production_sha=$PROD_SHA"
  echo "verified_candidate_sha=$CAND_SHA"
  echo "expected_build_sha=$EXPECTED_SHA"
  echo "scoring_policy=measured-variants+restricted-fixture-patterns-or-bounded-edit-distance(short=0,medium=1,long=2)"
  echo "play_dev=$PLAY_DEV"
  echo "selfcheck_attempts=$SELFCHECK_ATTEMPTS"
  echo "unseen_transcripts=$(grep -h -o '\"unseen_transcript\":true' "$OUT/turns.jsonl" "$OUT/selfcheck.jsonl" 2>/dev/null | wc -l)"
  for st in ${STAGES//,/ }; do echo "$st=${RES[$st]:-SKIP}"; done
  echo "frontend_sha=$($ADB shell "sha256sum /data/xiaorui-server/board-scripts/xiaorui_live" 2>/dev/null | tr -d '\r' | cut -c1-16)"
  echo "candidate_sha=$($ADB shell "sha256sum /data/xiaorui-gateway/xiaorui_live-cand" 2>/dev/null | tr -d '\r' | cut -c1-16)"
  echo "fails=$fails"
} > "$OUT/summary.txt"
cat "$OUT/summary.txt"
$ADB shell "mkdir -p /data/xiaorui-test-results" >/dev/null 2>&1
$ADB push "$OUT" /data/xiaorui-test-results/ >/dev/null 2>&1 && echo "[accept] 证据已镜像到板: /data/xiaorui-test-results/$(basename "$OUT")"
echo "[accept] 本机证据: $OUT"
[ "$fails" -eq 0 ]
