#!/bin/bash
# RK3562 板端音乐运行时部署: 在线电台(直链流式) + 本地离线库
#
# 装到板上的东西:
#   /data/xiaorui-gateway/deps/ffmpeg-min/ffmpeg    2MB 交叉编译 ffmpeg(解 HTTP 直链)
#   /usr/bin/ffmpeg -> 上面那个                     pydub 播本地 .mp3 时找得到
#   /data/xiaorui-gateway/server/music/              本地离线库(.p3 优先, 也能 .wav/.mp3)
#   /data/xiaorui-gateway/server/music-online/playlist.json  在线直链歌单(电台)
#   /data/xiaorui-gateway/work/{music,music-online}  软链(gateway 的 cwd 是 work/)
#   /data/xiaorui-gateway/data/.config.yaml         补 plugins.play_music 段
#
# 幂等: 重复跑只覆盖同名文件, 不动板上的离线库和自定义歌单(除非显式 --playlist)。
#
# 用法:
#   ports/rk3562/deploy-music.sh <serial> [--track <文件>] [--playlist <文件>] [--restart]
#
# 环境变量:
#   XR_FFMPEG_BIN  aarch64 交叉编译的 ffmpeg 路径(默认 ~/rk/ffmpeg-minimal-build/ffmpeg)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"

SERIAL=""
TRACK=""
PLAYLIST=""
RESTART=0
while [ $# -gt 0 ]; do
    case "$1" in
        --track) TRACK="$2"; shift 2 ;;
        --playlist) PLAYLIST="$2"; shift 2 ;;
        --restart) RESTART=1; shift ;;
        *) SERIAL="$1"; shift ;;
    esac
done
if [ -z "$SERIAL" ]; then
    # 注意: 不要用 XZ_BOARD_SERIAL —— 那个变量在这台机器上指的是另一块板。
    SERIAL="${XR_BOARD_SERIAL:-}"
fi
if [ -z "$SERIAL" ]; then
    SERIAL=$(adb devices | awk '/device$/{print $1}' | head -1)
fi
[ -n "$SERIAL" ] || { echo "没有在线 adb 设备(可传 serial 或设 XR_BOARD_SERIAL)" >&2; exit 2; }

FFMPEG_BIN="${XR_FFMPEG_BIN:-$HOME/rk/ffmpeg-minimal-build/ffmpeg}"
GW=/data/xiaorui-gateway
BOARD_FFMPEG="$GW/deps/ffmpeg-min/ffmpeg"

adb -s "$SERIAL" get-state >/dev/null 2>&1 || { echo "板子不在线: $SERIAL" >&2; exit 2; }
MODEL=$(adb -s "$SERIAL" shell 'cat /proc/device-tree/model' | tr -d '\0')
echo "== 板子: $SERIAL -> $MODEL"

# ---------- 1) ffmpeg ----------
if [ ! -x "$FFMPEG_BIN" ]; then
    echo "缺交叉编译的 ffmpeg: $FFMPEG_BIN" >&2
    echo "  在 SDK 主机上跑 tools/build-ffmpeg-min.sh 生成, 或设 XR_FFMPEG_BIN" >&2
    exit 2
fi
echo "== 1) ffmpeg =="
adb -s "$SERIAL" shell "mkdir -p $GW/deps/ffmpeg-min" >/dev/null
adb -s "$SERIAL" push "$FFMPEG_BIN" "$BOARD_FFMPEG" >/dev/null
adb -s "$SERIAL" shell "chmod +x $BOARD_FFMPEG" >/dev/null
adb -s "$SERIAL" shell "$BOARD_FFMPEG -version | head -1" | tr -d '\r'
# /usr/bin/ffmpeg 软链: pydub 播本地 .mp3 时按名字找 ffmpeg, 没有它 mp3 走不了
adb -s "$SERIAL" shell "ln -sfn $BOARD_FFMPEG /usr/bin/ffmpeg" >/dev/null

# ---------- 2) 目录 + 软链 ----------
echo "== 2) 音乐目录 =="
adb -s "$SERIAL" shell "mkdir -p $GW/server/music $GW/server/music-online $GW/work" >/dev/null
adb -s "$SERIAL" shell "ln -sfn $GW/server/music $GW/work/music; \
    ln -sfn $GW/server/music-online $GW/work/music-online" >/dev/null

# ---------- 3) 在线歌单 ----------
echo "== 3) 在线歌单 =="
if [ -n "$PLAYLIST" ]; then
    SRC="$PLAYLIST"
else
    SRC="$DIR/music-online.playlist.json"
fi
[ -f "$SRC" ] || { echo "缺歌单文件: $SRC" >&2; exit 2; }
adb -s "$SERIAL" push "$SRC" "$GW/server/music-online/playlist.json" >/dev/null
adb -s "$SERIAL" shell "cat $GW/server/music-online/playlist.json" | head -3 | tr -d '\r'

# ---------- 4) 网关配置 ----------
echo "== 4) 网关配置 plugins.play_music =="
CFG_LOCAL=$(mktemp)
trap 'rm -f "$CFG_LOCAL"' EXIT
adb -s "$SERIAL" shell "cat $GW/data/.config.yaml" > "$CFG_LOCAL"
python3 -I "$DIR/patch-music-config.py" --config "$CFG_LOCAL"
adb -s "$SERIAL" push "$CFG_LOCAL" "$GW/data/.config.yaml" >/dev/null
adb -s "$SERIAL" shell "grep -A4 '^plugins:' $GW/data/.config.yaml" | tr -d '\r'

# ---------- 5) 本地离线库 ----------
if [ -n "$TRACK" ]; then
    echo "== 5) 本地离线库: $TRACK =="
    NAME=$(basename "$TRACK")
    adb -s "$SERIAL" push "$TRACK" "$GW/server/music/$NAME" >/dev/null
    adb -s "$SERIAL" shell "ls -l $GW/server/music/" | tr -d '\r'
else
    echo "== 5) 本地离线库: 跳过(没传 --track) =="
fi

# ---------- 6) 重启 ----------
if [ "$RESTART" = "1" ]; then
    echo "== 6) 重启网关(ffmpeg 与配置都要重载才生效) =="
    # 只停网关: RKLLM 常驻 NPU 模型不动, 前端自带 WS 重连
    adb -s "$SERIAL" shell 'pkill -f "server/[a]pp.py" 2>/dev/null || true; sleep 2'
    adb -s "$SERIAL" shell "(setsid sh $GW/start-board-stack.sh >>$GW/data/stack.log 2>&1 </dev/null &); true"
    i=0
    while [ $i -lt 60 ]; do
        adb -s "$SERIAL" shell "$GW/probe-port.py 8001" >/dev/null 2>&1 && break
        sleep 2
        i=$((i + 1))
    done
    adb -s "$SERIAL" shell "$GW/probe-port.py 8001" >/dev/null 2>&1 \
        && echo "网关 :8001 就绪" || { echo "网关未就绪, 查 $GW/data/stack.log" >&2; exit 1; }
fi

echo "== 音乐运行时部署完成 =="
