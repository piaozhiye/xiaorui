#!/bin/bash
# Isolated dynamic FFmpeg + pinned MCP resolver. Does not restart any service.
set -euo pipefail
SERIAL=${XZ_BOARD_SERIAL:-}
ADB=(adb ${SERIAL:+-s $SERIAL})
MCP_REV=c99720807755049ff9e174631594972cc9ecdff5
WORK=$(mktemp -d /tmp/xr-music-runtime.XXXXXXXX)
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT
"${ADB[@]}" shell 'if ! /data/xiaorui-ffmpeg/bin/ffmpeg -version >/dev/null 2>&1; then /data/miniforge/bin/conda create -y -p /data/xiaorui-ffmpeg -c conda-forge ffmpeg=9.0.2; fi'
git clone --quiet https://github.com/ABUGG-007/xiaozhi-mcp-music.git "$WORK/mcp"
git -C "$WORK/mcp" checkout --quiet --detach "$MCP_REV"
"${ADB[@]}" shell 'mkdir -p /data/xiaorui-gateway/deps/music-mcp'
"${ADB[@]}" push "$WORK/mcp/music_mcp" /data/xiaorui-gateway/deps/music-mcp/ >/dev/null
"${ADB[@]}" push "$WORK/mcp/LICENSE" /data/xiaorui-gateway/deps/music-mcp/LICENSE >/dev/null
"${ADB[@]}" shell '/data/xiaorui-ffmpeg/bin/ffmpeg -version | sed -n "1,3p"'
echo "MCP revision=$MCP_REV; configuration example: board-server/music-mcp.example.json"
