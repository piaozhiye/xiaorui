#!/bin/sh
# 主机测试: speech_pipeline / voice_session / xr_vad (每拆一个模块追加一段)
set -eu
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -f "$TEST_DIR"/*; rmdir "$TEST_DIR"' EXIT
CC="${HOST_CC:-cc}"

"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/speech_pipeline.c" "$DIR/board/speech_pipeline_test.c" \
    -ldl -o "$TEST_DIR/speech"
"$TEST_DIR/speech"
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/speech_route_test.c" -o "$TEST_DIR/speech_route"
"$TEST_DIR/speech_route"

"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/voice_session.c" "$DIR/board/voice_session_test.c" \
    -o "$TEST_DIR/session"
"$TEST_DIR/session"

# 弃轮反馈音策略: 播失败/无音频时不得改会话状态(否则白��一段麦); 冷却只延长不缩短
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/voice_session.c" "$DIR/board/voice_session_cue_test.c" \
    -o "$TEST_DIR/session_cue"
"$TEST_DIR/session_cue"

# 慢响应填充音: 首包 2.58~3.16s, 到阈值还没音频就补"嗯"。每轮只许一声,
# 音频已开响则不补, begin_turn 必须重新武装
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/voice_session.c" "$DIR/board/voice_session_think_test.c" \
    -o "$TEST_DIR/session_think"
"$TEST_DIR/session_think"

"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/xr_vad.c" "$DIR/board/xr_vad_test.c" -lm -o "$TEST_DIR/vad"
"$TEST_DIR/vad"

# es8388 阶段2 采集适配: 通道抽取/抗混叠抽取(混叠抑制/连续性/边界)
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/xr_mic4.c" "$DIR/board/xr_mic4_test.c" -lm -o "$TEST_DIR/mic4"
"$TEST_DIR/mic4"
# 同套用例再过一遍 ASan+UBSan: 历史缓冲越界(30+60 反例)必须在这里爆, 不能等板上崩
"$CC" -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined \
    -I"$DIR/board" "$DIR/board/xr_mic4.c" "$DIR/board/xr_mic4_test.c" -lm \
    -o "$TEST_DIR/mic4_asan"
"$TEST_DIR/mic4_asan"

# 文本唤醒门: 纯头文件测试, 一直缺门禁, 本轮接入
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/xr_wake_test.c" -o "$TEST_DIR/wake"
"$TEST_DIR/wake"

# 语音音量控制: 意图判定(宁漏不抢)/步进/增益/持久化
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/xr_volume.c" "$DIR/board/xr_volume_test.c" -o "$TEST_DIR/volume"
"$TEST_DIR/volume"
"$CC" -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined \
    -I"$DIR/board" "$DIR/board/xr_volume.c" "$DIR/board/xr_volume_test.c" \
    -o "$TEST_DIR/volume_asan"
"$TEST_DIR/volume_asan"

"$CC" -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined \
    -I"$DIR/board" "$DIR/board/music_controls.c" "$DIR/board/music_controls_test.c" \
    "$DIR/board/xr_volume.c" -pthread -o "$TEST_DIR/music_controls"
"$TEST_DIR/music_controls"

"$CC" -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined \
    -I"$DIR/board" "$DIR/board/music_controls.c" "$DIR/board/music_controls_dump_test.c" \
    "$DIR/board/xr_volume.c" -pthread -Wl,--wrap=fopen,--wrap=fflush \
    -o "$TEST_DIR/music_dump"
for mode in unset empty missing write-fail normal; do
    "$TEST_DIR/music_dump" "$mode"
done

"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/playback_control.c" "$DIR/board/playback_control_test.c" \
    -o "$TEST_DIR/playback_control"
"$TEST_DIR/playback_control"
# JSON 字符串转义: ASR 文本原样进 listen 报文, 控制字符不转义=非法 JSON=整轮静默丢失
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -I"$DIR/board" \
    "$DIR/board/json_escape_test.c" -o "$TEST_DIR/json_escape"
"$TEST_DIR/json_escape"
python3 "$DIR/tests/test_capture_cadence.py"
python3 "$DIR/tests/test_cloud_only_acceptance.py"
python3 "$DIR/tests/test_wake_live_acceptance.py"
