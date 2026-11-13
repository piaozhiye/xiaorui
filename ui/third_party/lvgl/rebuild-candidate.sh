#!/bin/bash
set -euo pipefail

directory="$(cd "$(dirname "$0")" && pwd)"
mode="${1:?usage: rebuild-candidate.sh native-or-aarch64 /path/to/lvgl-git}"
source_repository="${2:?Provide a local upstream LVGL Git checkout}"
upstream_commit=4495f428630cc1741bd8bfd977f080e8460e8e8d
case "$mode" in
    native)
        compiler="${CC:-gcc}"
        architecture=x86
        read -r -a freetype_flags <<< "$(pkg-config --cflags freetype2)"
        flags=("${freetype_flags[@]}")
        ;;
    aarch64)
        toolchain="${XR_TOOLCHAIN_ROOT:-$HOME/projects/toolchains/gcc-arm-buildroot-aarch}"
        compiler="$toolchain/bin/aarch64-buildroot-linux-gnu-gcc"
        sysroot="$toolchain/aarch64-buildroot-linux-gnu/sysroot"
        export LD_LIBRARY_PATH="$toolchain/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        architecture=aarch64
        flags=(-march=armv8-a "--sysroot=$sysroot" "-I$sysroot/usr/include/freetype2"
               "-L$sysroot/usr/lib")
        ;;
    *) echo "unknown mode: $mode" >&2; exit 2 ;;
esac
git -C "$source_repository" cat-file -e "$upstream_commit^{commit}"
output_root="$directory/../../build/lvgl-candidate"
mkdir -p "$output_root"
stage="$(mktemp -d "$output_root/.build-$mode.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/source" "$stage/sdk/lib/$architecture"
git -C "$source_repository" archive "$upstream_commit" src lvgl.h LICENCE.txt \
    | tar -x -C "$stage/source"
cp "$directory/include/lv_conf.h" "$stage/source/lv_conf.h"
cp "$directory/include/lvgl/src/misc/lv_systick.h" "$stage/source/src/misc/lv_systick.h"
mapfile -t sources < <(rg --files "$stage/source/src" -g '*.c' | LC_ALL=C sort)
if [ "${#sources[@]}" -eq 0 ]; then
    echo 'no LVGL sources found' >&2
    exit 1
fi
command=("$compiler" -std=c11 -D_DEFAULT_SOURCE=1 -D_POSIX_C_SOURCE=200809L -O2 -fPIC -shared
         -DLV_CONF_INCLUDE_SIMPLE "-I$stage/source" "${flags[@]}"
         "${sources[@]}" -Wl,-z,defs -lfreetype -lm
         -o "$stage/sdk/lib/$architecture/liblvgl.so")
"${command[@]}"
python3 - "$stage/source" "$stage/sdk" <<'PY'
from pathlib import Path
import shutil
import sys

source, destination = map(Path, sys.argv[1:])
for header in source.rglob("*.h"):
    relative = header.relative_to(source)
    target = destination / "include" / relative if relative == Path("lv_conf.h") else (
        destination / "include/lvgl" / relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(header, target)
shutil.copyfile(source / "LICENCE.txt", destination / "LICENCE.txt")
PY
{
    echo 'Candidate SDK: not a reproduction of the shipped vendor binaries.'
    echo "Upstream commit: $upstream_commit"
    "$compiler" --version | head -1
    sha256sum "$stage/source/lv_conf.h" "$stage/source/src/misc/lv_systick.h"
    printf 'Command (temporary build paths): '
    printf '%q ' "${command[@]}"
    printf '\n'
} > "$stage/sdk/BUILD.txt"
destination="$output_root/$mode"
mkdir -p "$destination"
cp -R "$stage/sdk/." "$destination/"
echo "Candidate SDK: $(cd "$destination" && pwd)"
echo 'Use its headers and library together via XR_LVGL_ROOT; no shipped file was replaced.'
sha256sum "$destination/lib/$architecture/liblvgl.so"
