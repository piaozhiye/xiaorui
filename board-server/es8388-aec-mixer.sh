#!/bin/sh
# SAI1 SDI1 loopback, kernel 6.1.99 #69: PGA +24dB, source SDO0.
set -eu
amixer -c rockchipes8388 cset numid=52 8 >/dev/null
amixer -c rockchipes8388 cset numid=22 1 >/dev/null
amixer -c rockchipes8388 cset numid=18 0 >/dev/null
