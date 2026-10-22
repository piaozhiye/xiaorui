#include "runtime_config.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int env_integer(const char *name, int fallback, int low, int high) {
    const char *value = getenv(name);
    if (!value || !*value) return fallback;
    char *end;
    errno = 0;
    long parsed = strtol(value, &end, 10);
    if (errno || end == value || *end || parsed < low || parsed > high) {
        fprintf(stderr, "[config] %s 非法，使用默认值 %d\n", name, fallback);
        return fallback;
    }
    return (int)parsed;
}

const xr_runtime_config_t *xr_runtime_config(void) {
    static xr_runtime_config_t config;
    static int loaded;
    if (loaded) return &config;
    config.cloud_asr = env_integer("XR_CLOUD_ASR", 0, 0, 1);
    const char *local_asr = getenv("XR_LOCAL_ASR");
    config.local_asr = !local_asr || !*local_asr || !strcmp(local_asr, "rknn") ? 1
                       : !strcmp(local_asr, "off") ? 0 : -1;
    fprintf(stderr, "[config] local_asr=%s\n",
            config.local_asr == 1 ? "rknn" : config.local_asr == 0 ? "off" : "INVALID");
    config.music_controls = env_integer("XR_MUSIC_CONTROLS", 0, 0, 1);
    config.music_command_ms = env_integer("XR_MUSIC_COMMAND_MS", 2500, 1000, 5000);
    fprintf(stderr, "[config] music_controls=%d music_command_ms=%d\n",
            config.music_controls, config.music_command_ms);
    config.kws_trust_acoustic = env_integer("XR_KWS_TRUST_ACOUSTIC", 0, 0, 1);
    fprintf(stderr, "[config] cloud_asr=%d kws_trust_acoustic=%d\n",
            config.cloud_asr, config.kws_trust_acoustic);
    config.cloud_conn_ms = env_integer("XR_CLOUD_CONN_MS", 5000, 500, 15000);
    config.cloud_probe_s = env_integer("XR_CLOUD_PROBE_S", 60, 5, 3600);
    config.cloud_rx_dead_s = env_integer("XR_CLOUD_RX_DEAD_S", 75, 60, 3600);
    config.cloud_ping_failures = env_integer("XR_CLOUD_PING_FAILURES", 2, 1, 10);
    config.accepted_silence_s = env_integer("XR_ACCEPTED_SILENCE_S", 8, 1, 120);
    config.response_timeout_s = env_integer("XR_RESPONSE_TIMEOUT_S", 20, 1, 300);
    config.vad_preroll_frames = env_integer("XR_VAD_PREROLL", 5, 0, 30);
    config.vad_trigger_db = env_integer("XR_VAD_TRIGGER_DB", 6, 1, 20);
    config.vad_sustain_db = env_integer("XR_VAD_SUSTAIN_DB", 2, 0, 10);
    config.follow_window_s = env_integer("XR_FOLLOW_WINDOW_S", 30, 5, 300);
    /* 弃用长确认音 1.689s + cool 1500 的 3.19s 死窗; 改为短确认音 0.451s
       (板上 Matcha 合成的"我在", 与小爱同款) + cool 800, 死窗 1.25s。
       缩短 61%, 用户体感从"喊完要等 3 秒多"变成"刚听完确认音就能接着说"。
       余音风险: 短确认音的喇叭尾振也短, 800ms cool + drop_echo 兜底已知
       短余音词(好/好的/我/嗯/啊/哦/然后)足矣。实测:
         我在+cool 800, 1.5s 间隔 -> 成功; 1.0s 间隔 -> 失败(下界吻合)。
       老配置仍可现场覆盖: XR_ACK_COOL_MS=1500 配原 xr_ack24k.wav 即可回退。 */
    config.ack_cool_ms = env_integer("XR_ACK_COOL_MS", 800, 0, 10000);
    config.finish_cool_ms = env_integer("XR_FINISH_COOL_MS", 1200, 0, 10000);
    config.abandon_cool_ms = env_integer("XR_ABANDON_COOL_MS", 800, 0, 10000);
    config.fail_cue = env_integer("XR_FAIL_CUE", 1, 0, 1);
    config.fail_cue_cool_ms = env_integer("XR_FAIL_CUE_COOL_MS", 600, 0, 10000);
    /* 实测云端首包 2.58~3.16s, 用户要干等近 3 秒。本轮判断发生在 RESPOND 的
       轮询里(be_poll_event 超时 1000ms), 所以阈值必须小于 1000ms 才抢得到
       第一次轮询: 取 700ms, 首次轮询(~1.0s)若仍无音频就补, 实测由 2.74s 的
       静默缩到约 1.0s。音频已开响的快轮次(pkt_n>0)不补, 不会变噪音。
       XR_THINK_CUE_MS=0 现场可关。 */
    config.think_cue_ms = env_integer("XR_THINK_CUE_MS", 700, 0, 10000);
    config.echo_guard_s = env_integer("XR_ECHO_GUARD_S", 4, 0, 30);
    config.echo_min_cmd_chars = env_integer("XR_ECHO_MIN_CMD_CHARS", 3, 1, 10);
    /* 语音音量控制(本地意图, 不进 LLM): 判定器极保守(剥掉音量词后还有
       实义词的一律放行), 抢指令风险面很小; 仍留现场一键关。 */
    config.volume_control = env_integer("XR_VOLUME_CONTROL", 1, 0, 1);
    if (config.accepted_silence_s > config.response_timeout_s) {
        fprintf(stderr, "[config] 受理后静默阈值超过总等待阈值，恢复默认 8s/20s\n");
        config.accepted_silence_s = 8;
        config.response_timeout_s = 20;
    }
    loaded = 1;
    fprintf(stderr, "[config] cloud_conn=%dms probe=%ds rx_dead=%ds ping_failures=%d "
                    "accepted_silence=%ds response_timeout=%ds\n",
            config.cloud_conn_ms, config.cloud_probe_s, config.cloud_rx_dead_s,
            config.cloud_ping_failures, config.accepted_silence_s, config.response_timeout_s);
    fprintf(stderr, "[config] vad_preroll=%df trigger=+%ddb sustain=+%ddb follow_window=%ds "
                    "cool(ack/finish/abandon/fail)=%d/%d/%d/%dms echo_guard=%ds/%d字 "
                    "fail_cue=%d think_cue=%dms vol_ctl=%d\n",
            config.vad_preroll_frames, config.vad_trigger_db, config.vad_sustain_db,
            config.follow_window_s, config.ack_cool_ms, config.finish_cool_ms,
            config.abandon_cool_ms, config.fail_cue_cool_ms,
            config.echo_guard_s, config.echo_min_cmd_chars, config.fail_cue,
            config.think_cue_ms, config.volume_control);
    return &config;
}

int xr_runtime_config_validate(const xr_runtime_config_t *config, const char *backend) {
    if (config->local_asr != 0 && config->local_asr != 1) {
        fprintf(stderr, "[config] XR_LOCAL_ASR must be off or rknn\n");
        return -1;
    }
    if (!config->local_asr && (!backend || strcmp(backend, "cloud") || !config->cloud_asr)) {
        fprintf(stderr, "[config] XR_LOCAL_ASR=off requires XR_BACKEND=cloud XR_CLOUD_ASR=1\n");
        return -1;
    }
    return 0;
}
