#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
if [ -z "${CC:-}" ] && [ -z "${XR_TOOLCHAIN_ROOT:-}" ]; then
    echo 'Set CC or XR_TOOLCHAIN_ROOT for the target userspace ABI' >&2
    exit 2
fi
: "${XR_OPUS_ROOT:?Set XR_OPUS_ROOT to an Opus build for the same target ABI}"
exec sh "$ROOT/build-board-live.sh" "$@"
