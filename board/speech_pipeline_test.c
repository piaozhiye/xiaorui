/* speech_pipeline_test.c — fake sherpa ops 驱动的模块测试 (主机无 libsherpa) */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "speech_pipeline.h"

static int failures;
#define CHECK(cond, msg) do { \
    if (!(cond)) { failures++; fprintf(stderr, "FAIL %d: %s\n", __LINE__, (msg)); } \
} while (0)

/* ---- fake sherpa 状态 ---- */
static int g_lib_fail;              /* open_lib 返 NULL */
static const char *g_missing[16];   /* 需要解析失败的符号名 */
static int g_missing_n;
static int g_kws_ready;             /* is_ready 返回值(手动模式) */
static int g_kws_ready_after_feeds; /* 喂够 N 帧后 is_ready 返 1, N<=0 用手动模式 */
static int g_kws_feeds;
static int g_kws_resets;
static int g_kws_hits;
static char g_kw[64];               /* get_result 返回的 keyword */
static char g_json[512];            /* 离线结果 JSON */
static int g_cli_calls;
static int g_offline_symbols;
static int g_offline_creates;
static const char *g_cli_text;      /* fake CLI 返回文本, NULL=无 */

static void fake_reset(void) {
    xr_speech_destroy();   /* 清静态状态, 保证每个用例互不污染 */
    g_lib_fail = 0; g_missing_n = 0; g_kws_ready = 0; g_kws_ready_after_feeds = 0;
    g_kws_feeds = 0; g_kws_resets = 0; g_kws_hits = 0; g_kw[0] = 0;
    g_json[0] = 0; g_cli_calls = 0; g_cli_text = NULL;
    g_offline_symbols = 0; g_offline_creates = 0;
}

/* ---- fake sherpa 符号实现 (只经 ops 指针表调用, 签名与真实 ABI 对齐) ---- */
static void *f_kws_create(const void *cfg) { (void)cfg; return (void *)(size_t)1; }
static void f_kws_destroy(void *p) { (void)p; }
static void *f_kws_create_stream(const void *p) { (void)p; return (void *)(size_t)2; }
static void f_kws_destroy_stream(void *p) { (void)p; }
static int f_kws_is_ready(const void *p, const void *s) {
    (void)p; (void)s;
    if (g_kws_ready_after_feeds > 0) return g_kws_feeds >= g_kws_ready_after_feeds;
    return g_kws_ready;
}
static void f_kws_decode(const void *p, const void *s) {
    (void)p; (void)s; g_kws_hits++;
    /* 真实 sherpa: decode 后 stream 不再 ready */
    if (g_kws_ready_after_feeds <= 0) g_kws_ready = 0;
}
typedef struct { const char *keyword; char opaque[64]; } fake_result_t;
static fake_result_t g_result;
static const void *f_kws_get_result(const void *p, const void *s) {
    (void)p; (void)s; g_result.keyword = g_kw; return &g_result;
}
static void f_kws_destroy_result(const void *p) { (void)p; }
static void f_kws_reset(void *p, void *s) {
    (void)p; (void)s; g_kws_resets++; g_kws_feeds = 0;  /* reset 清累积 chunk */
}
static void f_accept_wave(void *s, int rate, const float *w, int n) {
    (void)s; (void)rate; (void)w; (void)n; g_kws_feeds++;
}
static void *f_off_create_recog(const void *cfg) {
    (void)cfg; ++g_offline_creates; return (void *)(size_t)3;
}
static void f_off_destroy_recog(void *p) { (void)p; }
static void *f_off_create_stream(const void *p) { (void)p; return (void *)(size_t)4; }
static void f_off_destroy_stream(void *p) { (void)p; }
static void f_off_accept_wave(void *s, int rate, const float *w, int n) {
    (void)s; (void)rate; (void)w; (void)n;
}
static void f_off_decode(void *r, void *s) { (void)r; (void)s; }
static const char *f_off_result_json(const void *s) { (void)s; return g_json; }
static void f_off_free_result_json(const char *s) { (void)s; }

/* ---- fake ops ---- */
static void *fake_open_lib(void) { return g_lib_fail ? NULL : (void *)(size_t)0xABCD; }
static void *fake_sym(void *lib, const char *name) {
    if (strstr(name, "Offline")) ++g_offline_symbols;
    struct { const char *n; void *p; } t[] = {
        {"SherpaOnnxOnlineStreamAcceptWaveform", (void *)f_accept_wave},
        {"SherpaOnnxCreateKeywordSpotter", (void *)f_kws_create},
        {"SherpaOnnxDestroyKeywordSpotter", (void *)f_kws_destroy},
        {"SherpaOnnxCreateKeywordStream", (void *)f_kws_create_stream},
        {"SherpaOnnxDestroyOnlineStream", (void *)f_kws_destroy_stream},
        {"SherpaOnnxIsKeywordStreamReady", (void *)f_kws_is_ready},
        {"SherpaOnnxDecodeKeywordStream", (void *)f_kws_decode},
        {"SherpaOnnxGetKeywordResult", (void *)f_kws_get_result},
        {"SherpaOnnxDestroyKeywordResult", (void *)f_kws_destroy_result},
        {"SherpaOnnxResetKeywordStream", (void *)f_kws_reset},
        {"SherpaOnnxCreateOfflineRecognizer", (void *)f_off_create_recog},
        {"SherpaOnnxDestroyOfflineRecognizer", (void *)f_off_destroy_recog},
        {"SherpaOnnxCreateOfflineStream", (void *)f_off_create_stream},
        {"SherpaOnnxDestroyOfflineStream", (void *)f_off_destroy_stream},
        {"SherpaOnnxAcceptWaveformOffline", (void *)f_off_accept_wave},
        {"SherpaOnnxDecodeOfflineStream", (void *)f_off_decode},
        {"SherpaOnnxGetOfflineStreamResultAsJson", (void *)f_off_result_json},
        {"SherpaOnnxDestroyOfflineStreamResultJson", (void *)f_off_free_result_json},
    };
    (void)lib;
    for (int i = 0; i < g_missing_n; i++)
        if (!strcmp(g_missing[i], name)) return NULL;
    for (size_t i = 0; i < sizeof(t)/sizeof(t[0]); i++)
        if (!strcmp(t[i].n, name)) return t[i].p;
    return NULL;   /* 未知符号: 对应现状 dlsym 失败路径 */
}
static int fake_cli(const int16_t *pcm, size_t n, char *out, size_t cap) {
    (void)pcm; (void)n;
    g_cli_calls++;
    if (!g_cli_text) return 0;   /* 跑过但无文本 (对应写 wav/起进程失败返 -1 的语义) */
    snprintf(out, cap, "%s", g_cli_text);
    return 0;
}
static const xr_speech_ops_t FAKE_OPS = {fake_open_lib, fake_sym, fake_cli};

static int16_t frame[1600];
static void fill_frame(int16_t v) { for (int i = 0; i < 1600; i++) frame[i] = v; }

int main(void) {
    char text[1024];

    /* 1. dlopen 失败 => init -1 (main 会 return 2) */
    fake_reset(); g_lib_fail = 1;
    CHECK(xr_speech_init(&FAKE_OPS) == -1, "dlopen 失败时 init 应返 -1");
    CHECK(xr_speech_kws_available() == 0, "init 失败后 KWS 不可用");

    /* 2. KWS 符号缺失 => 非致命, 能量门回路, feed/flush 空安全 */
    fake_reset(); g_missing_n = 1; g_missing[0] = "SherpaOnnxCreateKeywordSpotter";
    CHECK(xr_speech_init(&FAKE_OPS) == 0, "KWS 符号缺失时 init 不应致命");
    CHECK(xr_speech_kws_available() == 0, "KWS 符号缺失时 kws_available 应为 0");
    fill_frame(1000);
    CHECK(xr_speech_kws_feed(frame, 1600) == 0, "KWS 不可用时 feed 应返 0");
    CHECK(xr_speech_kws_flush() == 0, "KWS 不可用时 flush 应返 0");
    xr_speech_kws_reset();

    /* 3. 命中一次 => 命中后已 reset, 后续帧不再命中 */
    fake_reset();
    CHECK(xr_speech_init(&FAKE_OPS) == 0, "全符号时 init 应为 0");
    CHECK(xr_speech_kws_available() == 1, "全符号时 kws_available 应为 1");
    snprintf(g_kw, sizeof g_kw, "小瑞小瑞");
    g_kws_ready_after_feeds = 2;      /* 第 2 帧开始 ready */
    CHECK(xr_speech_kws_feed(frame, 1600) == 0, "未 ready 时不应命中");
    CHECK(xr_speech_kws_feed(frame, 1600) == 1, "ready 后应命中");
    CHECK(g_kws_hits == 1, "命中后应已 decode 一次");
    CHECK(g_kws_resets == 1, "命中后应立即 reset");
    CHECK(xr_speech_kws_feed(frame, 1600) == 0, "命中并 reset 后不应重复命中");

    /* 4. 空 keyword => 不算命中; flush 无论命中与否都 reset */
    fake_reset();
    CHECK(xr_speech_init(&FAKE_OPS) == 0, "init 应为 0");
    g_kws_ready = 1; g_kw[0] = 0;
    CHECK(xr_speech_kws_feed(frame, 1600) == 0, "空 keyword 不应算命中");
    g_kw[0] = 0; g_kws_ready = 1; g_kws_resets = 0;
    CHECK(xr_speech_kws_flush() == 0, "空 keyword 的 flush 不应命中");
    CHECK(g_kws_resets == 1, "flush 无论命中与否都应 reset");

    /* 5. flush 命中 */
    fake_reset();
    CHECK(xr_speech_init(&FAKE_OPS) == 0, "init 应为 0");
    snprintf(g_kw, sizeof g_kw, "小瑞小瑞"); g_kws_ready = 1;
    CHECK(xr_speech_kws_flush() == 1, "ready 时 flush 应命中");

    /* 6. recognize: inproc 出文 + 尾部语气词剥离 (剥离作用于句尾, 与现状一致) */
    snprintf(g_json, sizeof g_json, "{\"text\": \"好的好的嗯\"}");
    CHECK(xr_speech_recognize(frame, 1600, text, sizeof(text)) > 0, "inproc 应识别出文本");
    CHECK(!strcmp(text, "好的好的"), "句尾语气词 嗯 应被剥离");
    CHECK(g_cli_calls == 0, "inproc 出文时不应调 CLI");

    /* 7. recognize: 标点/尾空格剥离 */
    snprintf(g_json, sizeof g_json, "{\"text\":\"今天天气不错。 \"}");
    xr_speech_recognize(frame, 1600, text, sizeof text);
    CHECK(!strcmp(text, "今天天气不错"), "尾部空白与句号应被剥离");

    /* 8. recognize: inproc 空 => CLI 回退 */
    snprintf(g_json, sizeof g_json, "{\"text\":\"\"}");
    g_cli_text = "小瑞小瑞讲个笑话";
    CHECK(xr_speech_recognize(frame, 1600, text, sizeof text) > 0, "应走 CLI 回退");
    CHECK(g_cli_calls == 1, "CLI 应被调用一次");
    CHECK(!strcmp(text, "小瑞小瑞讲个笑话"), "应取 CLI 文本");

    /* 9. recognize: 两路皆空 */
    g_json[0] = 0; g_cli_text = NULL;
    CHECK(xr_speech_recognize(frame, 1600, text, sizeof text) == 0, "两路皆空应返 0");
    CHECK(text[0] == 0, "两路皆空时 out 应为空串");

    /* 10. XR_CLI_REFINE 强制走 CLI */
    snprintf(g_json, sizeof g_json, "{\"text\":\"你好\"}");
    g_cli_text = "命令行结果";
    g_cli_calls = 0;
    setenv("XR_CLI_REFINE", "1", 1);
    CHECK(xr_speech_recognize(frame, 1600, text, sizeof(text)) > 0, "XR_CLI_REFINE 应识别");
    CHECK(g_cli_calls == 1, "XR_CLI_REFINE 时应调 CLI");
    CHECK(!strcmp(text, "命令行结果"), "XR_CLI_REFINE 时应取 CLI 文本");
    unsetenv("XR_CLI_REFINE");

    /* KWS-only runtime: no offline API is even looked up; CLI is forbidden. */
    fake_reset();
    xr_speech_config_t cloud = {.local_asr = 0, .kws_dir = "/only-kws"};
    CHECK(xr_speech_init_config(&FAKE_OPS, &cloud) == 0, "KWS-only init should work");
    CHECK(xr_speech_kws_available(), "cloud-only keeps CPU KWS");
    CHECK(g_offline_symbols == 0 && g_offline_creates == 0,
          "off must not bind or create offline recognizer");
    g_cli_text = "must not be used";
    setenv("XR_CLI_REFINE", "1", 1);
    snprintf(text, sizeof text, "old text");
    CHECK(xr_speech_recognize(frame, 1600, text, sizeof text) < 0,
          "recognize explicitly unavailable with local ASR off");
    CHECK(text[0] == 0 && g_cli_calls == 0, "off must clear text without spawning CLI");
    unsetenv("XR_CLI_REFINE");
    snprintf(g_kw, sizeof g_kw, "小瑞小瑞"); g_kws_ready = 1;
    CHECK(xr_speech_kws_flush() == 1, "KWS still decodes with local ASR off");
    fake_reset(); g_missing_n = 1; g_missing[0] = "SherpaOnnxCreateKeywordSpotter";
    CHECK(xr_speech_init_config(&FAKE_OPS, &cloud) < 0, "KWS-only fails if KWS missing");
    CHECK(g_offline_symbols == 0, "KWS failure must not enable local ASR");
    fake_reset();
    CHECK(xr_speech_init(&FAKE_OPS) == 0 && g_offline_symbols > 0,
          "legacy init must still initialize local ASR");

    xr_speech_destroy();
    fprintf(stderr, failures ? "speech_pipeline_test: %d FAILED\n" : "speech_pipeline_test: OK\n",
            failures);
    return failures ? 1 : 0;
}
