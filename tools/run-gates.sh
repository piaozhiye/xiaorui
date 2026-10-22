#!/bin/bash
# 主机门禁总入口: 板端联网网关轮的全部确定性回归.
# 每个子门禁独立断言非零退出; 总报告不把 SKIP 算 PASS.
# 用法: tools/run-gates.sh [子集…]   (无参=全部)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# 测试解释器可配置: 公开克隆自建 venv 后 export XZ_TEST_VENV=<python>;
# 缺省为维护机路径, 不存在时明确报错而不是各门禁各自 cryptic 失败。
VENV="${XZ_TEST_VENV:-$HOME/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python}"
if [ ! -x "$VENV" ]; then
    echo "测试解释器不存在: $VENV" >&2
    echo "自建 venv 并 export XZ_TEST_VENV=<python> 后重试(测试依赖见各 tests/test_*.py 导入)" >&2
    exit 1
fi
PASS=0; FAIL=0; FAILED=""

run() {  # run <名> <命令...>
    local name="$1"; shift
    echo "==== $name ===="
    if "$@"; then
        PASS=$((PASS+1)); echo "---- $name PASS"
    else
        local rc=$?    # 先抓住退出码: 下面的赋值会把 $? 冲掉
        FAIL=$((FAIL+1)); FAILED="$FAILED $name"; echo "---- $name FAIL (exit=$rc)"
    fi
}

ALL="test-backends test-voice test-audio-player volume-intent ha-intent timer-intent timer-corpus session-coordinator timer-store gateway-runtime radio-intent time-intent repeat-intent voiceprint-intent next-intent audio-cue json-escape music-stream p3-decode tracked-refs build-board-live build-board-audio-probe no-lse board-gateway board-acceptance vqe-probe vqe-acceptance diff-check"
ALL="$ALL soak-health live-smoke ha-acceptance noise-gate wakeup-config wake-strip silent-drop wake-shape short-text model-downloads weather-credentials logger-fallback"
ONLY="${*:-$ALL}"

for g in $ONLY; do
    case "$g" in
        model-downloads)       run "$g" python3 "$REPO/tests/test_download_models.py" ;;
        weather-credentials)   run "$g" python3 "$REPO/tests/test_weather_credentials.py" ;;
        logger-fallback)       run "$g" "$VENV" "$REPO/tests/test_logger_fallback.py" ;;
        ha-acceptance)        run "$g" python3 "$REPO/tests/test_ha_acceptance.py" ;;
        noise-gate)            run "$g" "$VENV" "$REPO/tests/test_noise_gate.py" ;;
        wakeup-config)         run "$g" python3 "$REPO/tests/test_wakeup_config.py" ;;
        wake-strip)            run "$g" "$VENV" "$REPO/tests/test_wake_strip.py" ;;
        silent-drop)           run "$g" "$VENV" "$REPO/tests/test_silent_drop.py" ;;
        wake-shape)            run "$g" "$VENV" "$REPO/tests/test_wake_shape.py" ;;
        short-text)            run "$g" "$VENV" "$REPO/tests/test_short_text_gate.py" ;;
        soak-health)         run "$g" python3 "$REPO/tests/test_soak_health.py" ;;
        live-smoke)          run "$g" python3 "$REPO/tests/test_live_smoke.py" ;;
        test-backends)        run "$g" bash -c "cd '$REPO' && ./test-backends.sh" ;;
        test-voice)           run "$g" bash -c "cd '$REPO' && ./test-voice.sh" ;;
        test-audio-player)    run "$g" bash -c "cd '$REPO' && ./test-audio-player.sh" ;;
        audio-cue)            run "$g" python3 "$REPO/tests/test_audio_cue.py" ;;
        volume-intent)        run "$g" python3 "$REPO/tests/test_volume_intent.py" ;;
        ha-intent)            run "$g" python3 "$REPO/tests/test_ha_intent.py" ;;
        timer-intent)        run "$g" python3 "$REPO/tests/test_timer_intent.py" ;;
        timer-corpus)        run "$g" python3 "$REPO/tests/test_timer_corpus.py" ;;
        session-coordinator) run "$g" python3 "$REPO/tests/test_session_coordinator.py" ;;
        timer-store)         run "$g" python3 "$REPO/tests/test_timer_store.py" ;;
        gateway-runtime)     run "$g" python3 "$REPO/tests/test_gateway_runtime.py" ;;
        radio-intent)        run "$g" python3 "$REPO/tests/test_radio_intent.py" ;;
        time-intent)         run "$g" python3 "$REPO/tests/test_time_intent.py" ;;
        repeat-intent)       run "$g" python3 "$REPO/tests/test_repeat_intent.py" ;;
        voiceprint-intent)   run "$g" python3 "$REPO/tests/test_voiceprint_intent.py" ;;
        next-intent)         run "$g" python3 "$REPO/tests/test_next_intent.py" ;;
        music-stream)         run "$g" bash -c '"$1" "$2/tests/test_music_stream.py" && python3 "$2/tests/test_music_yield_probe.py" && python3 "$2/tests/test_music_candidate.py"' _ "$VENV" "$REPO" ;;
        p3-decode)            run "$g" python3 "$REPO/tests/test_p3.py" ;;
        json-escape)          run "$g" python3 "$REPO/tests/test_json_escape.py" ;;
        tracked-refs)        run "$g" python3 "$REPO/tools/check_tracked_refs.py" ;;
        build-board-live)     run "$g" bash -c "cd '$REPO' && ./build-board-live.sh" ;;
        build-board-audio-probe) run "$g" bash -c "cd '$REPO' && ./build-board-audio-probe.sh" ;;
        no-lse)               run "$g" bash "$REPO/tools/check-no-lse.sh" ;;
        board-gateway)        run "$g" "$VENV" "$REPO/tests/test_board_gateway.py" ;;
        board-acceptance)     run "$g" python3 "$REPO/tests/test_board_acceptance.py" ;;
        vqe-probe)            if [ -d "$REPO/vendor/rkaudio-vqe" ]; then run "$g" python3 "$REPO/tests/test_vqe_probe.py"; else echo "---- vqe-probe SKIP (vendor/rkaudio-vqe 本地维护件未发布)"; fi ;;
        vqe-acceptance)       if [ -d "$REPO/vendor/rkaudio-vqe" ]; then run "$g" python3 "$REPO/tests/test_vqe_acceptance_lifecycle.py"; else echo "---- vqe-acceptance SKIP (vendor/rkaudio-vqe 本地维护件未发布)"; fi ;;
        diff-check)           run "$g" git -C "$REPO" diff --check ;;
        *) echo "未知门禁: $g"; exit 2 ;;
    esac
done

echo "================"
echo "GATES pass=$PASS fail=$FAIL${FAILED:+ failed:$FAILED}"
[ "$FAIL" -eq 0 ]
