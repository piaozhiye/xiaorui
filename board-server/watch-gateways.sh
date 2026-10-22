#!/bin/sh
# Both independent instances are watched. A failure is observable, not masked.
set -u
ROOT=${XR_GATEWAY_ROOT:-/data/xiaorui-gateway}
rc=0
sh "$ROOT/start-gateway.sh" || rc=1
sh "$ROOT/start-gateway-8002.sh" || rc=1
exit "$rc"
