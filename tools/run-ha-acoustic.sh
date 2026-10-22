#!/bin/bash
# Requires HA_AUDIT gateway logs; old regex-only scoring is not accepted.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/run-ha-acoustic.py" "$@"
