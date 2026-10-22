#!/bin/sh
set -eu
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -f "$TEST_DIR/ws" "$TEST_DIR/hybrid" "$TEST_DIR/gateway" "$TEST_DIR/config"; rmdir "$TEST_DIR"' EXIT
HOST_CC="${HOST_CC:-cc}"
"$HOST_CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/runtime_config.c" "$DIR/board/runtime_config_test.c" -o "$TEST_DIR/config"
for mode in default custom invalid cross off off-invalid bad-local; do
    "$TEST_DIR/config" "$mode"
done
"$HOST_CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/ws_transport.c" "$DIR/board/ws_transport_test.c" -o "$TEST_DIR/ws"
"$HOST_CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/backend_hybrid.c" "$DIR/board/backend_hybrid_test.c" \
    "$DIR/board/runtime_config.c" -pthread -o "$TEST_DIR/hybrid"
"$HOST_CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    -I"${XR_OPUS_ROOT:-/tmp/opus-aarch64}/include/opus" \
    "$DIR/board/backend_local.c" "$DIR/board/backend_cloud.c" \
    "$DIR/board/ws_transport.c" "$DIR/board/runtime_config.c" \
    "$DIR/board/backend_gateway_test.c" -l:libopus.so.0 -o "$TEST_DIR/gateway"
"$TEST_DIR/ws"
"$TEST_DIR/hybrid"
python3 "$DIR/tests/fake_gateway.py" "$TEST_DIR/gateway"
python3 "$DIR/tests/test_manual_audio_order.py"
