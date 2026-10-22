#!/bin/bash
# 候选架构声学验收: 前端=xiaorui_live-arch(18001/18002), 语料=tests/audio wake2.
# 判据: 前端 /tmp/xr_arch.log + 双候选网关日志. 全程真实出声(PC->USB麦->ASR).
# 注意: 网关日志偏移必须用各自文件的 gwpos, 不能复用前端 logpos(会匹配历史行).
set -u
cd "$(dirname "$0")/.."
AUDIO=tests/audio
PLAY_DEV="${PLAY_DEV:-plughw:1,0}"
SER=${XZ_BOARD_SERIAL:-}
XRLOG=/tmp/xr_arch.log
G1LOG=/data/xiaorui-gateway/instances/cand-18001/work/tmp/server.log
G2LOG=/data/xiaorui-gateway/instances/cand-18002/work/tmp/server.log
PASS=0; FAIL=0

board() { adb ${SER:+-s $SER} shell "$1"; }
logpos() { board "wc -c <$XRLOG 2>/dev/null" | tr -d '\r\n '; }
gwpos() { board "wc -c <$1 2>/dev/null" | tr -d '\r\n '; }
play() { aplay -D "$PLAY_DEV" -q "$AUDIO/$1"; }
wait_pat() { # wait_pat <log> <pattern> <timeout_s> <from_offset>
    local log="$1" pat="$2" tmo=$3 off=$4 i=0 p
    local deadline=$((SECONDS+tmo))
    while [ $SECONDS -lt $deadline ]; do
        p=$(board "wc -c <$log 2>/dev/null" | tr -d '\r\n ')
        if [ "$p" -gt "$off" ] 2>/dev/null; then
            if board "tail -c +$((off+1)) $log 2>/dev/null | grep -Eqa \"$pat\""; then return 0; fi
        fi
        sleep 2
    done
    return 1
}
wait_any() { # wait_any <pat> <tmo> <off1> <log1> <off2> <log2>
    local pat="$1" tmo=$2 off log p
    local deadline=$((SECONDS+tmo))
    while [ $SECONDS -lt $deadline ]; do
        off="$3"; log="$4"
        p=$(board "wc -c <$log 2>/dev/null" | tr -d '\r\n ')
        if [ "$p" -gt "$off" ] 2>/dev/null && \
           board "tail -c +$((off+1)) $log 2>/dev/null | grep -Eqa \"$pat\""; then return 0; fi
        off="$5"; log="$6"
        p=$(board "wc -c <$log 2>/dev/null" | tr -d '\r\n ')
        if [ "$p" -gt "$off" ] 2>/dev/null && \
           board "tail -c +$((off+1)) $log 2>/dev/null | grep -Eqa \"$pat\""; then return 0; fi
        sleep 2
    done
    return 1
}
ok()   { PASS=$((PASS+1)); echo "PASS $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL $1"; }
settle() { board "sleep ${1:-15}"; }

echo "== R1 报时(云端腿) =="
off=$(logpos); play wake2_time.wav
wait_pat "$XRLOG" "回答完毕" 30 $off && ok "R1-time" || bad "R1-time"

echo "== R2 长回答(允许两次: 唤醒词偶发被精修吞, 属 ASR 抖动) =="
r2_ok=0
for attempt in 1 2; do
    settle
    off=$(logpos); play wake2_story.wav
    if wait_pat "$XRLOG" "回答完毕" 90 $off; then r2_ok=1; break; fi
    echo "R2 attempt$attempt no turn; retrying"
done
[ $r2_ok -eq 1 ] && ok "R2-story" || bad "R2-story"

echo "== R3 定时30s到点主动播报 =="
settle
o1=$(gwpos "$G1LOG"); o2=$(gwpos "$G2LOG"); off=$(logpos)
play wake2_timerset30w.wav
wait_pat "$XRLOG" "回答完毕" 40 $off || bad "R3-set-reply"
off=$(logpos)
wait_pat "$XRLOG" "主动播报已受理" 60 $off && ok "R3-offer-accepted" || bad "R3-offer-accepted"
off_done=$(logpos)
wait_any "timer delivery.*success=True" 120 "$o1" "$G1LOG" "$o2" "$G2LOG" \
    && ok "R3-delivery" || bad "R3-delivery"
wait_pat "$XRLOG" "回答完毕" 10 $off_done && ok "R3-announce-done" || bad "R3-announce-done"

echo "== R4 电台播放+控制线程快停(1.2s 间隙语料, 控制窗=KWS后2500ms) =="
settle
o1=$(gwpos "$G1LOG")
off=$(logpos); play wake2_radio.wav
wait_pat "$G1LOG" "music stream first_audio" 60 $o1 && ok "R4-radio-streaming" || bad "R4-radio-streaming"
wait_pat "$XRLOG" "已启用\(音乐\)" 30 $off && ok "R4-media-flag" || bad "R4-media-flag"
board "sleep 8"
off=$(logpos); play wake2_stopfast.wav
wait_pat "$XRLOG" "语音停止播放" 40 $off && ok "R4-voice-stop" || bad "R4-voice-stop"

echo "== R5 电台中控制线程快音量 =="
settle
o1=$(gwpos "$G1LOG")
off=$(logpos); play wake2_radio.wav
wait_pat "$G1LOG" "music stream first_audio" 60 $o1 || bad "R5-radio-start2"
board "sleep 8"
off=$(logpos); play wake2_volfast.wav
wait_pat "$XRLOG" "播放中音量|控制识别" 40 $off && ok "R5-volume" || bad "R5-volume"
off=$(logpos); play wake2_stopfast.wav
wait_pat "$XRLOG" "语音停止播放" 40 $off || bad "R5-cleanup-stop"

echo "== R6 无唤醒负例(窗已过期) =="
board "sleep 35"
off=$(logpos); play xr_ck_set1.wav
wait_pat "$XRLOG" "未含唤醒词" 25 $off && ok "R6-bare-ignored" || bad "R6-bare-ignored"

echo "== R7 断云走本地腿(允许两次: 现场语音可能盖唤醒词) =="
board 'kill $(cat /data/xiaorui-gateway/instances/cand-18001/run/server.pid) 2>/dev/null'
sleep 3
r7_ok=0
for attempt in 1 2; do
    settle
    o2=$(gwpos "$G2LOG"); off=$(logpos)
    play wake2_time.wav
    if wait_pat "$G2LOG" "收到listen消息" 90 $o2 && wait_pat "$XRLOG" "回答完毕" 60 $off; then r7_ok=1; break; fi
    echo "R7 attempt$attempt no local turn; retrying"
done
[ $r7_ok -eq 1 ] && ok "R7-local-leg" || bad "R7-local-leg"

echo "== R8 断云期设定时, 云恢复后单投递 =="
settle
c1=$(board "grep -ac 'timer delivery.*success=True' $G1LOG" | tr -d '\r\n ')
c2=$(board "grep -ac 'timer delivery.*success=True' $G2LOG" | tr -d '\r\n ')
off=$(logpos); play wake2_timerset30w.wav
wait_pat "$XRLOG" "回答完毕" 40 $off || bad "R8-set-reply"
board 'sh /data/xiaorui-gateway/start-gateway-instance.sh cand-18001' >/dev/null
o1b=$(gwpos "$G1LOG"); o2b=$(gwpos "$G2LOG"); off_done=$(logpos)
wait_any "timer delivery.*success=True" 120 "$o1b" "$G1LOG" "$o2b" "$G2LOG" || bad "R8-delivery"
sleep 20
d1=$(( $(board "grep -ac 'timer delivery.*success=True' $G1LOG" | tr -d '\r\n ') - c1 ))
d2=$(( $(board "grep -ac 'timer delivery.*success=True' $G2LOG" | tr -d '\r\n ') - c2 ))
if [ $((d1+d2)) -eq 1 ]; then ok "R8-single-delivery(cloud+$d1 local+$d2)"; else bad "R8-deliveries=${d1}+${d2}"; fi
wait_pat "$XRLOG" "回答完毕" 10 $off_done && ok "R8-announce-done" || bad "R8-announce-done"
settle 10
off=$(logpos); play wake2_time.wav
wait_pat "$XRLOG" "回答完毕" 40 $off && ok "R8-back-to-cloud" || bad "R8-back-to-cloud"

echo "================"
echo "ACOUSTIC pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
