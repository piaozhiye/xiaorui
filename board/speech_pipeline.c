/* speech_pipeline.c — KWS + 离线 RKNN 精修 + CLI 回退 + 尾音剥离. */
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "xr_audio_format.h"
#include "speech_pipeline.h"
#include "xr_offline_shim.h"

/* ---------- sherpa C API 结构体 (照 c-api 1.13.8 布局, 照搬 xiaorui_live.c:49-98) ---------- */
typedef struct { int32_t sample_rate, feature_dim; } xr_feat_t;
typedef struct { const char *encoder, *decoder; } xr_para_t;
typedef struct { const char *encoder, *decoder, *joiner; } xr_trans_t;
typedef struct { const char *model; } xr_one_t;
typedef struct {
    xr_trans_t transducer; xr_para_t paraformer; xr_one_t zipformer2_ctc;
    const char *tokens; int32_t num_threads; const char *provider;
    int32_t debug; const char *model_type; const char *modeling_unit;
    const char *bpe_vocab; const char *tokens_buf; int32_t tokens_buf_size;
    xr_one_t nemo_ctc; xr_one_t t_one_ctc;
} xr_model_t;
typedef struct { const char *graph; int32_t max_active; } xr_ctcfst_t;
typedef struct { const char *dict_dir, *lexicon, *rule_fsts; } xr_hr_t;
typedef struct {
    xr_feat_t feat; xr_model_t model;
    const char *decoding_method; int32_t max_active_paths;
    int32_t enable_endpoint; float rule1, rule2, rule3;
    const char *hotwords_file; float hotwords_score;
    xr_ctcfst_t ctc_fst; const char *rule_fsts, *rule_fars;
    float blank_penalty; const char *hotwords_buf; int32_t hotwords_buf_size;
    xr_hr_t hr;
} xr_recog_cfg_t;
/* KeywordSpotterConfig: feat + OnlineModelConfig(=xr_model_t 布局) + 尾字段, v1.13.8 */
typedef struct {
    xr_feat_t feat;
    xr_model_t model;
    int32_t max_active_paths;
    int32_t num_trailing_blanks;
    float keywords_score;
    float keywords_threshold;
    const char *keywords_file;
    const char *keywords_buf;
    int32_t keywords_buf_size;
} xr_kws_cfg_t;
/* KeywordResult: 只读首字段 keyword(指针), 其余不透明 */
typedef struct { const char *keyword; char opaque[64]; } xr_kws_result_t;

static const char *SHERPA_SONAME = "libsherpa-onnx-c-api.so";
#define KWS_DIR "/data/rknn-test/sherpa/model-kws"
#define OFF_DIR "/data/rknn-test/sherpa/model-20s"
#define CLI_WAV_PATH "/tmp/xr_utt.wav"

static const xr_speech_ops_t *g_ops;
static int local_asr_enabled;
static const char *library_path;
static const char *kws_dir;
static char kws_paths[5][1024];
static void *sherpa_lib;
static void (*s_accept_wave)(void *, int, const float *, int);

static void *(*kws_create)(const void *);
static void (*kws_destroy)(void *);
static void *(*kws_create_stream)(const void *);
static void (*kws_destroy_stream)(void *);
static int (*kws_is_ready)(const void *, const void *);
static void (*kws_decode)(const void *, const void *);
static const void *(*kws_get_result)(const void *, const void *);
static void (*kws_destroy_result)(const void *);
static void (*kws_reset)(void *, void *);

static void *(*off_create_recog)(const SherpaOnnxOfflineRecognizerConfig *);
static void (*off_destroy_recog)(void *);
static void *(*off_create_stream)(const void *);
static void (*off_destroy_stream)(void *);
static void (*off_accept_wave)(void *, int, const float *, int);
static void (*off_decode)(void *, void *);
static const char *(*off_result_json)(const void *);
static void (*off_free_result_json)(const char *);

static void *kws_spotter, *kws_stream, *off_recog;
static float feed_buf[FRAME_SAMPLES];
static float recog_buf[FRAME_SAMPLES];

static double now_s(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

/* ---------- 板上默认 ops (现状硬编码路径, 值逐字照搬) ---------- */
static void *default_open_lib(void) {
    void *h = dlopen(library_path ? library_path : SHERPA_SONAME, RTLD_NOW);
    if (!h) fprintf(stderr, "[xr] dlopen sherpa: %s\n", dlerror());
    return h;
}
static void *default_sym(void *lib, const char *name) {
    void *p = dlsym(lib, name);
    if (!p) fprintf(stderr, "[xr] dlsym %s: %s\n", name, dlerror());
    return p;
}
static int default_cli_refine(const int16_t *pcm, size_t n, char *out, size_t cap) {
    unsigned char wh[44] = {
        'R','I','F','F', 0,0,0,0, 'W','A','V','E','f','m','t',' ',
        16,0,0,0, 1,0, 1,0, 0x80,0x3e,0,0, 0,0x7d,0,0, 2,0, 16,0,
        'd','a','t','a', 0,0,0,0 };
    uint32_t dl2 = (uint32_t)(n * 2);
    uint32_t rf2 = 36 + dl2, sr2 = 16000, br2 = 32000;
    memcpy(wh + 4, &rf2, 4); memcpy(wh + 24, &sr2, 4);
    memcpy(wh + 28, &br2, 4); memcpy(wh + 40, &dl2, 4);
    FILE *wf = fopen(CLI_WAV_PATH, "wb");
    if (!wf) return -1;
    fwrite(wh, 1, 44, wf);
    fwrite(pcm, 2, n, wf);
    fclose(wf);
    char cmd[1024];
    snprintf(cmd, sizeof cmd,
             "cd /data/rknn-test/sherpa && "
             "LD_LIBRARY_PATH=$PWD/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib:/usr/lib "
             "$PWD/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/bin/sherpa-onnx-offline "
             "--tokens=./model-20s/tokens.txt "
             "--paraformer=./model-20s/encoder.rknn,./model-20s/predictor.rknn,./model-20s/decoder.rknn "
             "--provider=rknn --model-type=paraformer " CLI_WAV_PATH " 2>/dev/null");
    FILE *pp = popen(cmd, "r");
    if (!pp) return -1;
    char line[4096];
    while (fgets(line, sizeof(line), pp)) {
        char *t = strstr(line, "\"text\": \"");
        if (t) {
            t += 9;
            size_t ti = 0;
            while (*t && *t != '"' && ti < cap - 1) out[ti++] = *t++;
            out[ti] = 0;
        }
    }
    pclose(pp);
    return 0;
}
static const xr_speech_ops_t DEFAULT_OPS = {default_open_lib, default_sym, default_cli_refine};
const xr_speech_ops_t *xr_speech_default_ops(void) { return &DEFAULT_OPS; }

/* ---------- 绑定与初始化 (失败语义照搬现状: dlopen 致命, KWS/offline 非致命) ---------- */
static int bind_symbols(void **fn, const char *name) {
    *fn = g_ops->sym(sherpa_lib, name);
    return *fn ? 0 : -1;   /* 错误日志由 ops.sym 打印, 保持现状一行输出 */
}
static int kws_bind(void) {
    struct { const char *n; void **p; } t[] = {
        {"SherpaOnnxCreateKeywordSpotter", (void**)&kws_create},
        {"SherpaOnnxDestroyKeywordSpotter", (void**)&kws_destroy},
        {"SherpaOnnxCreateKeywordStream", (void**)&kws_create_stream},
        {"SherpaOnnxDestroyOnlineStream", (void**)&kws_destroy_stream},
        {"SherpaOnnxIsKeywordStreamReady", (void**)&kws_is_ready},
        {"SherpaOnnxDecodeKeywordStream", (void**)&kws_decode},
        {"SherpaOnnxGetKeywordResult", (void**)&kws_get_result},
        {"SherpaOnnxDestroyKeywordResult", (void**)&kws_destroy_result},
        {"SherpaOnnxResetKeywordStream", (void**)&kws_reset},
    };
    for (size_t i = 0; i < sizeof(t)/sizeof(t[0]); i++)
        if (bind_symbols(t[i].p, t[i].n)) return -1;
    return 0;
}
static int offline_bind(void) {
    struct { const char *n; void **p; } t[] = {
        {"SherpaOnnxCreateOfflineRecognizer", (void**)&off_create_recog},
        {"SherpaOnnxDestroyOfflineRecognizer", (void**)&off_destroy_recog},
        {"SherpaOnnxCreateOfflineStream", (void**)&off_create_stream},
        {"SherpaOnnxDestroyOfflineStream", (void**)&off_destroy_stream},
        {"SherpaOnnxAcceptWaveformOffline", (void**)&off_accept_wave},
        {"SherpaOnnxDecodeOfflineStream", (void**)&off_decode},
        {"SherpaOnnxGetOfflineStreamResultAsJson", (void**)&off_result_json},
        {"SherpaOnnxDestroyOfflineStreamResultJson", (void**)&off_free_result_json},
    };
    for (size_t i = 0; i < sizeof(t)/sizeof(t[0]); i++)
        if (bind_symbols(t[i].p, t[i].n)) return -1;
    return 0;
}
static int kws_init(void) {   /* 照搬 xiaorui_live.c:102-150 */
    if (kws_bind()) return -1;
    const char *files[] = {"encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                          "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                          "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
                          "tokens.txt", "keywords.txt"};
    for (size_t i = 0; i < 5; ++i) {
        int n = snprintf(kws_paths[i], sizeof(kws_paths[i]), "%s/%s", kws_dir, files[i]);
        if (n < 0 || (size_t)n >= sizeof(kws_paths[i])) {
            fprintf(stderr, "[xr] KWS model path too long\n");
            return -1;
        }
    }
    xr_kws_cfg_t cfg; memset(&cfg, 0, sizeof(cfg));
    cfg.feat.sample_rate = RATE; cfg.feat.feature_dim = 80;
    cfg.model.transducer.encoder = kws_paths[0];
    cfg.model.transducer.decoder = kws_paths[1];
    cfg.model.transducer.joiner = kws_paths[2];
    cfg.model.tokens = kws_paths[3];
    cfg.model.num_threads = 1;
    cfg.model.provider = "cpu";
    cfg.max_active_paths = 4;
    cfg.num_trailing_blanks = 1;
    cfg.keywords_score = 1.25f;
    cfg.keywords_threshold = 0.15f;   /* 2026-10-11 深夜定案: 0.15 下 120 负例两轮全量
                                         零误唤醒(声学近音命中全被文本门拦下), 正例召回
                                         走 句首/句中双单元 双路径; XR_KWS_THRESHOLD 可覆盖 */
    const char *kws_threshold = getenv("XR_KWS_THRESHOLD");
    if (kws_threshold && *kws_threshold) {
        char *end = NULL;
        float value = strtof(kws_threshold, &end);
        if (end != kws_threshold && *end == 0 && value >= 0.01f && value <= 0.5f)
            cfg.keywords_threshold = value;
        else
            fprintf(stderr, "[xr] 忽略非法 XR_KWS_THRESHOLD=%s\n", kws_threshold);
    }
    cfg.keywords_file = kws_paths[4];
    kws_spotter = kws_create(&cfg);
    if (!kws_spotter) { fprintf(stderr, "[xr] kws init failed\n"); return -1; }
    kws_stream = kws_create_stream(kws_spotter);
    if (!kws_stream) { kws_destroy(kws_spotter); kws_spotter = NULL; return -1; }
    fprintf(stderr, "[xr] kws(3.3M zipformer) ready threshold=%.2f\n", cfg.keywords_threshold);
    return 0;
}
static int offline_init(void) {   /* 照搬 xiaorui_live.c:180-199 */
    if (offline_bind()) return -1;
    SherpaOnnxOfflineRecognizerConfig cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.feat_config.sample_rate = RATE;
    cfg.feat_config.feature_dim = 80;
    cfg.model_config.paraformer.model = OFF_DIR "/encoder.rknn," OFF_DIR "/predictor.rknn," OFF_DIR "/decoder.rknn";
    cfg.model_config.tokens = OFF_DIR "/tokens.txt";
    cfg.model_config.num_threads = 2;
    cfg.model_config.provider = "rknn";
    cfg.model_config.model_type = "paraformer";
    cfg.decoding_method = "greedy_search";
    off_recog = off_create_recog(&cfg);
    if (!off_recog) { fprintf(stderr, "[xr] offline recog init failed\n"); return -1; }
    fprintf(stderr, "[xr] offline(20s RKNN) ready\n");
    return 0;
}

int xr_speech_init(const xr_speech_ops_t *ops) {
    const xr_speech_config_t config = {.local_asr = 1};
    return xr_speech_init_config(ops, &config);
}

int xr_speech_init_config(const xr_speech_ops_t *ops, const xr_speech_config_t *config) {
    if (!config || (config->local_asr != 0 && config->local_asr != 1)) return -1;
    local_asr_enabled = config->local_asr;
    kws_dir = config->kws_dir && *config->kws_dir ? config->kws_dir : KWS_DIR;
    library_path = config->library_path && *config->library_path
                   ? config->library_path : SHERPA_SONAME;
    g_ops = ops ? ops : xr_speech_default_ops();
    sherpa_lib = g_ops->open_lib();
    if (!sherpa_lib) return -1;
    if (bind_symbols((void **)&s_accept_wave, "SherpaOnnxOnlineStreamAcceptWaveform")) return -1;
    int kws_rc = kws_init(); /* legacy local mode keeps energy-gate fallback */
    if (kws_rc && !local_asr_enabled) {
        fprintf(stderr, "[xr] cloud-only requires a working CPU KWS\n");
        return -1;
    }
    if (local_asr_enabled) offline_init();
    else fprintf(stderr, "[xr] local ASR disabled: no offline symbols/models/CLI\n");
    fprintf(stderr, "[xr] speech capabilities: kws=%d local_asr=%s kws_dir=%s\n",
            kws_stream != NULL, local_asr_enabled ? "rknn/cli" : "off", kws_dir);
    return 0;
}
void xr_speech_destroy(void) {
    if (kws_stream) { kws_destroy_stream(kws_stream); kws_stream = NULL; }
    if (kws_spotter) { kws_destroy(kws_spotter); kws_spotter = NULL; }
    if (off_recog) { off_destroy_recog(off_recog); off_recog = NULL; }
    g_ops = NULL;
    local_asr_enabled = 0;
    sherpa_lib = NULL;   /* 句柄不 dlclose: 现状进程内常驻到退出 */
}

int xr_speech_kws_available(void) { return kws_stream != NULL; }

void xr_speech_kws_reset(void) {
    if (kws_stream) kws_reset(kws_spotter, kws_stream);
}

/* 照搬 xiaorui_live.c:476-489 的喂帧+收割循环; 命中日志与内部 reset 行为不变 */
int xr_speech_kws_feed(const int16_t *pcm, int n) {
    if (!kws_stream) return 0;
    if (n > FRAME_SAMPLES) n = FRAME_SAMPLES;
    for (int k = 0; k < n; k++) feed_buf[k] = pcm[k] / 32768.0f;
    s_accept_wave(kws_stream, RATE, feed_buf, n);
    int hit = 0;
    while (kws_is_ready(kws_spotter, kws_stream)) {
        kws_decode(kws_spotter, kws_stream);
        const xr_kws_result_t *kr = kws_get_result(kws_spotter, kws_stream);
        if (kr && kr->keyword && kr->keyword[0]) {
            fprintf(stderr, "[xr] KWS 命中: %s\n", kr->keyword);
            hit = 1;
        }
        if (kr) kws_destroy_result(kr);
        if (hit) { kws_reset(kws_spotter, kws_stream); break; }
    }
    return hit;
}

/* 照搬 xiaorui_live.c:552-561: is_ready 才 decode, 无论命中与否都 reset */
int xr_speech_kws_flush(void) {
    if (!kws_stream) return 0;
    int hit = 0;
    if (kws_is_ready(kws_spotter, kws_stream)) {
        kws_decode(kws_spotter, kws_stream);
        const xr_kws_result_t *kr = kws_get_result(kws_spotter, kws_stream);
        if (kr && kr->keyword && kr->keyword[0]) {
            fprintf(stderr, "[xr] KWS 命中(句尾): %s\n", kr->keyword);
            hit = 1;
        }
        if (kr) kws_destroy_result(kr);
    }
    kws_reset(kws_spotter, kws_stream);
    return hit;
}

/* 照搬 xiaorui_live.c:574-658: inproc 精修 -> 空则 CLI -> 尾部语气词/标点剥离 */
int xr_speech_recognize(const int16_t *pcm, size_t n, char *out, size_t cap) {
    if (cap == 0) return -1;
    out[0] = 0;
    if (!local_asr_enabled) return -1; /* includes forced XR_CLI_REFINE: never spawn */
    if (off_recog && !getenv("XR_CLI_REFINE")) {
        double t_r = now_s();
        void *ost = off_create_stream(off_recog);
        if (ost) {
            for (size_t off = 0; off < n; off += FRAME_SAMPLES) {
                size_t cnt = n - off < FRAME_SAMPLES ? n - off : FRAME_SAMPLES;
                for (size_t k = 0; k < cnt; k++) recog_buf[k] = pcm[off + k] / 32768.0f;
                off_accept_wave(ost, RATE, recog_buf, (int)cnt);
            }
            off_decode(off_recog, ost);
            const char *ojson = off_result_json(ost);
            if (ojson) {
                const char *k2 = strstr(ojson, "\"text\":");
                if (k2) {
                    k2 += 7;
                    while (*k2 == ' ') k2++;
                    if (*k2 == '"') k2++;
                    size_t ti = 0;
                    while (*k2 && *k2 != '"' && ti < cap - 1) out[ti++] = *k2++;
                    out[ti] = 0;
                }
            }
            if (ojson) off_free_result_json(ojson);
            off_destroy_stream(ost);
        }
        fprintf(stderr, "[xr] ASR 精修(20s RKNN): %s (%.2fs)\n",
                out[0] ? out : "(空)", now_s() - t_r);
    }
    if (!out[0]) {
        double t_r = now_s();
        if (g_ops->cli_refine(pcm, n, out, cap) == 0)
            fprintf(stderr, "[xr] ASR 精修(CLI 20s): %s (%.1fs)\n",
                    out[0] ? out : "(空)", now_s() - t_r);
    }
    /* 尾部语气词剥离: 喇叭回声常在句尾带入 嗯/啊 (20s 模型会把尾音识成语气词) */
    size_t tl = strlen(out);
    while (tl >= 3) {
        if (!strncmp(out + tl - 3, "嗯", 3) ||
            !strncmp(out + tl - 3, "啊", 3) ||
            !strncmp(out + tl - 3, "呃", 3)) { out[tl - 3] = 0; tl -= 3; }
        else break;
    }
    while (tl && (out[tl-1] == ' ' || out[tl-1] == '.')) out[--tl] = 0;
    while (tl >= 3 && (!strncmp(out + tl - 3, "，", 3) ||
                       !strncmp(out + tl - 3, "。", 3))) out[tl - 3] = 0, tl -= 3;
    return out[0] ? 1 : 0;
}
