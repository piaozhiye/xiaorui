#define _POSIX_C_SOURCE 200809L
#include "runtime_config.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    assert(argc == 2);
    const char *names[] = {"XR_CLOUD_CONN_MS", "XR_CLOUD_PROBE_S", "XR_CLOUD_RX_DEAD_S",
                          "XR_CLOUD_PING_FAILURES", "XR_ACCEPTED_SILENCE_S",
                          "XR_RESPONSE_TIMEOUT_S", "XR_VAD_PREROLL", "XR_VAD_TRIGGER_DB",
                          "XR_VAD_SUSTAIN_DB", "XR_FOLLOW_WINDOW_S", "XR_ACK_COOL_MS",
                          "XR_FINISH_COOL_MS", "XR_ABANDON_COOL_MS", "XR_ECHO_GUARD_S", "XR_ECHO_MIN_CMD_CHARS", "XR_VOLUME_CONTROL"};
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); ++i)
        assert(unsetenv(names[i]) == 0);
    assert(unsetenv("XR_CLOUD_ASR") == 0);
    assert(unsetenv("XR_LOCAL_ASR") == 0);
    if (!strcmp(argv[1], "off") || !strcmp(argv[1], "off-invalid")
        || !strcmp(argv[1], "bad-local")) {
        assert(setenv("XR_LOCAL_ASR", !strcmp(argv[1], "bad-local") ? "typo" : "off", 1) == 0);
        assert(setenv("XR_CLOUD_ASR", "1", 1) == 0);
        const xr_runtime_config_t *off = xr_runtime_config();
        if (!strcmp(argv[1], "bad-local")) {
            assert(xr_runtime_config_validate(off, "cloud") < 0);
        } else {
            assert(off->local_asr == 0);
            assert(xr_runtime_config_validate(off, "cloud") == 0);
            assert(xr_runtime_config_validate(off, "local") < 0);
            assert(xr_runtime_config_validate(off, "hybrid") < 0);
            assert(xr_runtime_config_validate(off, NULL) < 0);
            xr_runtime_config_t text = *off;
            text.cloud_asr = 0;
            assert(xr_runtime_config_validate(&text, "cloud") < 0);
        }
        return 0;
    }
    int custom = !strcmp(argv[1], "custom");
    if (custom) {
        assert(setenv("XR_CLOUD_ASR", "1", 1) == 0);
        assert(setenv(names[0], "1200", 1) == 0);
        assert(setenv(names[1], "90", 1) == 0);
        assert(setenv(names[2], "120", 1) == 0);
        assert(setenv(names[3], "3", 1) == 0);
        assert(setenv(names[4], "12", 1) == 0);
        assert(setenv(names[5], "30", 1) == 0);
        assert(setenv(names[6], "8", 1) == 0);
        assert(setenv(names[7], "9", 1) == 0);
        assert(setenv(names[8], "3", 1) == 0);
        assert(setenv(names[9], "60", 1) == 0);
        assert(setenv(names[10], "2000", 1) == 0);
        assert(setenv(names[11], "1500", 1) == 0);
        assert(setenv(names[12], "1200", 1) == 0);
        assert(setenv(names[13], "6", 1) == 0);
        assert(setenv(names[14], "2", 1) == 0);
        assert(setenv(names[13], "6", 1) == 0);
        assert(setenv(names[14], "2", 1) == 0);
        assert(setenv(names[15], "0", 1) == 0);
    } else if (!strcmp(argv[1], "invalid")) {
        assert(setenv("XR_CLOUD_ASR", "2", 1) == 0);
        assert(setenv(names[0], "1200junk", 1) == 0);
        assert(setenv(names[1], "999999999999999999999999", 1) == 0);
        assert(setenv(names[2], "-1", 1) == 0);
        assert(setenv(names[3], "0", 1) == 0);
        assert(setenv(names[4], "abc", 1) == 0);
        assert(setenv(names[5], "301", 1) == 0);
        assert(setenv(names[6], "31", 1) == 0);       /* 预滚超上限 */
        assert(setenv(names[7], "0", 1) == 0);        /* 触发余量下界以下 */
        assert(setenv(names[9], "3", 1) == 0);        /* 跟说窗下界以下 */
        assert(setenv(names[10], "-5", 1) == 0);
        assert(setenv(names[13], "31", 1) == 0);
        assert(setenv(names[13], "31", 1) == 0);   /* 护栏窗超上限 */
        assert(setenv(names[15], "2", 1) == 0);    /* 音量开关越界 -> 默认开 */
    } else if (!strcmp(argv[1], "cross")) {
        assert(setenv(names[4], "30", 1) == 0);
        assert(setenv(names[5], "10", 1) == 0);
    } else assert(!strcmp(argv[1], "default"));
    const xr_runtime_config_t *c = xr_runtime_config();
    assert(c->local_asr == 1 && xr_runtime_config_validate(c, NULL) == 0);
    assert(c->cloud_asr == (custom ? 1 : 0));
    assert(c->cloud_conn_ms == (custom ? 1200 : 5000));
    assert(c->cloud_probe_s == (custom ? 90 : 60));
    assert(c->cloud_rx_dead_s == (custom ? 120 : 75));
    assert(c->cloud_ping_failures == (custom ? 3 : 2));
    assert(c->accepted_silence_s == (custom ? 12 : 8));
    assert(c->response_timeout_s == (custom ? 30 : 20));
    assert(c->vad_preroll_frames == (custom ? 8 : 5));
    assert(c->vad_trigger_db == (custom ? 9 : 6));
    assert(c->vad_sustain_db == (custom ? 3 : 2));
    assert(c->follow_window_s == (custom ? 60 : 30));
    assert(c->ack_cool_ms == (custom ? 2000 : 800));
    assert(c->finish_cool_ms == (custom ? 1500 : 1200));
    assert(c->abandon_cool_ms == (custom ? 1200 : 800));
    assert(c->echo_guard_s == (custom ? 6 : 4));
    assert(c->echo_min_cmd_chars == (custom ? 2 : 3));
    assert(c->echo_guard_s == (custom ? 6 : 4));
    assert(c->echo_min_cmd_chars == (custom ? 2 : 3));
    assert(c->volume_control == (custom ? 0 : 1));
    /* 环境改变不会在运行中改变已加载的策略。 */
    assert(setenv(names[0], "9000", 1) == 0);
    assert(xr_runtime_config() == c && c->cloud_conn_ms == (custom ? 1200 : 5000));
    return 0;
}
