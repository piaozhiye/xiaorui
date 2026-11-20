#!/bin/sh
# 停掉整套小瑞(UI + 语音栈 + HA), 回到桌面。
#
# 桌面图标只有「小瑞」一个(退出用小瑞界面自己的退出按钮), 本脚本是
# 运维/应急入口: adb shell sh /data/xiaorui-ui/xiaorui-stop.sh
#
# ★ 必须撤掉 crond 里的 HA 看护行
#   板上 crontab 有 */2 * * * * .../start-ha.sh, 不撤的话 2 分钟后
#   HA 又被拉起来 —— "退出"就不彻底了。S98 的 start 分支会重新装回
#   这行, 所以下次点「小瑞」图标时 HA 照常自启。
#
# ★ 进程要停干净: SIGTERM 后 LLM 要几秒卸模型, 网关也会留一会儿,
#   只发信号不等待的话, "退出"后后台还在占着 NPU 内存。
set -u

echo "[xiaorui-stop] 停止小瑞整套..."

# 按 cmdline 精确找进程并发信号。不用 pkill -f(会连承载它的 shell
# 一起匹配, 自己把自己杀了)。
kill_matching() {
    pattern="$1"
    sig="$2"
    for dir in /proc/[0-9]*; do
        pid=${dir#/proc/}
        [ "$pid" != "$$" ] || continue
        [ "$pid" != "$PPID" ] || continue
        [ -r "$dir/cmdline" ] || continue
        cmd=$(tr '\000' ' ' < "$dir/cmdline" 2>/dev/null)
        case "$cmd" in
            *"$pattern"*) kill "-$sig" "$pid" 2>/dev/null || true ;;
        esac
    done
}

alive_matching() {
    pattern="$1"
    for dir in /proc/[0-9]*; do
        pid=${dir#/proc/}
        [ "$pid" != "$$" ] || continue
        [ "$pid" != "$PPID" ] || continue
        [ -r "$dir/cmdline" ] || continue
        cmd=$(tr '\000' ' ' < "$dir/cmdline" 2>/dev/null)
        case "$cmd" in
            *"$pattern"*) return 0 ;;
        esac
    done
    return 1
}

# 1) 触摸界面
if [ -f /etc/init.d/S99xiaorui-ui ]; then
    sh /etc/init.d/S99xiaorui-ui stop 2>/dev/null || true
fi

# 2) 语音栈: 前端 / 网关 / LLM / rock ASR+TTS helper
if [ -f /etc/init.d/S98xiaorui ]; then
    sh /etc/init.d/S98xiaorui stop 2>/dev/null || true
fi

# 3) HA 本体
kill_matching "-m homeassistant" TERM

# 4) 撤掉 HA 的 crond 看护(否则 2 分钟后复活)
crontab -l 2>/dev/null | grep -v 'xiaorui-server/start-ha.sh' | crontab - 2>/dev/null || true

# 5) 等它们真正退出, 超时硬停 —— 语音栈卸模型要几秒, 不等就是
#    "退出"了但后台还占着 NPU 内存。
i=0
while [ $i -lt 20 ]; do
    still=0
    for pattern in "/data/xiaorui-server/llm_rkllm.py" \
                   "/data/xiaorui-gateway/server/app.py" \
                   "rock/asr_helper.py" "rock/tts_helper.py" \
                   "-m homeassistant"; do
        if alive_matching "$pattern"; then still=1; break; fi
    done
    [ $still -eq 0 ] && break
    sleep 1
    i=$((i + 1))
done
if [ $i -ge 20 ]; then
    echo "[xiaorui-stop] 仍有残留, 强制结束" >&2
    for pattern in "/data/xiaorui-server/llm_rkllm.py" \
                   "/data/xiaorui-gateway/server/app.py" \
                   "rock/asr_helper.py" "rock/tts_helper.py" \
                   "-m homeassistant"; do
        kill_matching "$pattern" KILL
    done
fi

echo "[xiaorui-stop] 已停止(HA 看护已撤, 下次启动图标会自动装回)"
