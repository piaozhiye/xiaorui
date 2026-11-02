#!/bin/bash
# Minimal FFmpeg (4M, built by tools/build-ffmpeg-min.sh on the SDK host) + pinned
# MCP resolver. Does not restart any service — XR_FFMPEG takes effect on next
# gateway restart.
set -euo pipefail
SERIAL=${XZ_BOARD_SERIAL:-}
ADB=(adb ${SERIAL:+-s $SERIAL})
MCP_REV=c99720807755049ff9e174631594972cc9ecdff5
BIN_DIR=${1:-$HOME/rk/ffmpeg-minimal-build}
WORK=$(mktemp -d /tmp/xr-music-runtime.XXXXXXXX)
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT

[ -x "$BIN_DIR/ffmpeg" ] && [ -x "$BIN_DIR/ffprobe" ] || {
  echo "缺少 $BIN_DIR/ffmpeg|ffprobe：先在 SDK 主机跑 tools/build-ffmpeg-min.sh" >&2
  exit 1
}

"${ADB[@]}" shell 'mkdir -p /data/xiaorui-gateway/deps/ffmpeg-min'
"${ADB[@]}" push "$BIN_DIR/ffmpeg" "$BIN_DIR/ffprobe" /data/xiaorui-gateway/deps/ffmpeg-min/ >/dev/null
"${ADB[@]}" shell 'chmod +x /data/xiaorui-gateway/deps/ffmpeg-min/*
  for I in 8001 8002; do
    E=/data/xiaorui-gateway/instances/$I/runtime.env
    [ -f "$E" ] && grep -q "^XR_FFMPEG=" "$E" || \
      echo "XR_FFMPEG=/data/xiaorui-gateway/deps/ffmpeg-min/ffmpeg" >> "$E"
  done
  ln -sfn /data/xiaorui-gateway/deps/ffmpeg-min/ffmpeg /usr/bin/ffmpeg
  /usr/bin/ffmpeg -version | sed -n 1p'

git clone --quiet https://github.com/ABUGG-007/xiaozhi-mcp-music.git "$WORK/mcp"
git -C "$WORK/mcp" checkout --quiet --detach "$MCP_REV"
"${ADB[@]}" shell 'mkdir -p /data/xiaorui-gateway/deps/music-mcp'
"${ADB[@]}" push "$WORK/mcp/music_mcp" /data/xiaorui-gateway/deps/music-mcp/ >/dev/null
"${ADB[@]}" push "$WORK/mcp/LICENSE" /data/xiaorui-gateway/deps/music-mcp/LICENSE >/dev/null
echo "MCP revision=$MCP_REV; XR_FFMPEG written to both instances (restart gateways to apply);"
echo "configuration example: board-server/music-mcp.example.json"
