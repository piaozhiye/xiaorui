#!/bin/sh
# Two-mic + two-reference capture on ES8388:
#   SDI0 = codec microphone L/R; SDI1 = internal SAI loopback from playback SDO0.
# Keep SDI0 loopback disabled so the reference never replaces the mic lanes.
# Kernel 6.1.99 #69. Do not use the unsupported SDI LR MIC/LP split control here.
set -eu
amixer -c rockchipes8388 cset name='Left Channel Capture Volume' 8 >/dev/null
amixer -c rockchipes8388 cset name='SAI1 SDI0 Loopback Switch' 0 >/dev/null
amixer -c rockchipes8388 cset name='SAI1 SDI1 Loopback Src Select' 'From SDO0' >/dev/null
amixer -c rockchipes8388 cset name='SAI1 SDI1 Loopback Switch' 1 >/dev/null
