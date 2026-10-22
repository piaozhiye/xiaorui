/* 最小 VQE 节点探针: 完全模仿 xiaorui_live 的采集模式(dlopen libasound + 自建
 * 函数指针表 + readi 循环), 排除前端业务代码干扰, 定位同进程崩溃归属。 */
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "speech_pipeline.h"
#include "backend.h"
#include "audio_player.h"
#include <opus.h>

typedef struct _snd_pcm snd_pcm_t;
typedef long sframes_t;

static int (*a_open)(snd_pcm_t **, const char *, int, int);
static int (*a_hwp_malloc)(void **);
static int (*a_hwp_any)(snd_pcm_t *, void *);
static int (*a_set_access)(snd_pcm_t *, void *, unsigned);
static int (*a_set_format)(snd_pcm_t *, void *, unsigned);
static int (*a_set_channels)(snd_pcm_t *, void *, unsigned);
static int (*a_set_rate)(snd_pcm_t *, void *, unsigned, int);
static int (*a_set_ps)(snd_pcm_t *, void *, unsigned long, int);
static int (*a_set_periods)(snd_pcm_t *, void *, unsigned, int);
static int (*a_hwp)(snd_pcm_t *, void *);
static void (*a_hwp_free)(void *);
static int (*a_prepare)(snd_pcm_t *);
static sframes_t (*a_readi)(snd_pcm_t *, void *, unsigned long);
static int (*a_drop)(snd_pcm_t *);
static int (*a_close)(snd_pcm_t *);
static const char *(*a_strerror)(int);

static double now(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    const char *dev = argc > 1 ? argv[1] : "rkaudio_vqe_48k";
    int secs = argc > 2 ? atoi(argv[2]) : 60;
    void *h = dlopen("libasound.so.2", RTLD_NOW);
    if (!h) { fprintf(stderr, "dlopen: %s\n", dlerror()); return 1; }
    struct { const char *n; void **p; } t[] = {
        {"snd_pcm_open", (void **)&a_open}, {"snd_pcm_hw_params_malloc", (void **)&a_hwp_malloc},
        {"snd_pcm_hw_params_any", (void **)&a_hwp_any}, {"snd_pcm_hw_params_set_access", (void **)&a_set_access},
        {"snd_pcm_set_format", (void **)&a_set_format}, /* 占位防拼写, 下一行才是真 */
        {NULL, NULL},
    };
    (void)t;
    #define LOAD(v, n) do { v = dlsym(h, n); if (!v) { fprintf(stderr, "dlsym %s\n", n); return 1; } } while (0)
    LOAD(a_open, "snd_pcm_open"); LOAD(a_hwp_malloc, "snd_pcm_hw_params_malloc");
    LOAD(a_hwp_any, "snd_pcm_hw_params_any"); LOAD(a_set_access, "snd_pcm_hw_params_set_access");
    LOAD(a_set_format, "snd_pcm_hw_params_set_format"); LOAD(a_set_channels, "snd_pcm_hw_params_set_channels");
    LOAD(a_set_rate, "snd_pcm_hw_params_set_rate"); LOAD(a_set_ps, "snd_pcm_hw_params_set_period_size");
    LOAD(a_set_periods, "snd_pcm_hw_params_set_periods"); LOAD(a_hwp, "snd_pcm_hw_params");
    LOAD(a_hwp_free, "snd_pcm_hw_params_free"); LOAD(a_prepare, "snd_pcm_prepare");
    LOAD(a_readi, "snd_pcm_readi"); LOAD(a_drop, "snd_pcm_drop"); LOAD(a_close, "snd_pcm_close");
    LOAD(a_strerror, "snd_strerror");

    /* 复刻前端时序0: 先加载 sherpa/rknn 库(前端进程常态) */
    if (getenv("PROBE_OPUS")) {
        void *ol = dlopen("libopus.so.0", RTLD_NOW);
        fprintf(stderr, "probe: dlopen libopus.so.0 ok=%d\n", !!ol);
        if (ol) {
            void *(*dec_create)(int, int, int *) = dlsym(ol, "opus_decoder_create");
            int err = 0;
            void *dec = dec_create ? dec_create(24000, 1, &err) : NULL;
            fprintf(stderr, "probe: opus_decoder_create=%p err=%d\n", dec, err);
        }
    }
    
    if (getenv("PROBE_DEC")) {
            int de = 0;
            OpusDecoder *dec = opus_decoder_create(24000, 1, &de);
            fprintf(stderr, "probe: static opus decoder=%p err=%d\n", (void*)dec, de);
        }
        if (getenv("PROBE_ENC")) {
        int oe = 0;
        OpusEncoder *enc = opus_encoder_create(16000, 1, OPUS_APPLICATION_VOIP, &oe);
        fprintf(stderr, "probe: static opus encoder=%p err=%d\n", (void*)enc, oe);
        if (enc) opus_encoder_ctl(enc, OPUS_SET_BITRATE(24000));
    }
    if (getenv("PROBE_SHERPA")) {
        const char *libs[] = {
            "/data/rknn-test/sherpa/sherpa-onnx-v1.13.8-rknn-linux-aarch64-shared/lib/libsherpa-onnx-c-api.so",
            "/usr/lib/librknnrt.so", NULL,
        };
        for (int i = 0; libs[i]; i++) {
            void *sl = dlopen(libs[i], RTLD_NOW | RTLD_GLOBAL);
            fprintf(stderr, "probe: dlopen %s → %s\n", libs[i], sl ? "ok" : dlerror());
        }
    }
    /* 复刻前端时序: 先开播放通道 xr_spk(不写), 再开 VQE 采集 */
    if (getenv("PROBE_PLAY24")) {   /* play-first 24k/1ch 经 xr_spk plug, 再开 VQE 采集 */
        static int (*p_open)(snd_pcm_t **, const char *, int, int);
        LOAD(p_open, "snd_pcm_open");
        snd_pcm_t *pb = NULL;
        int rcp = p_open(&pb, "xr_spk", 0, 0);
        void *phw = NULL;
        if (rcp == 0) {
            a_hwp_malloc(&phw); a_hwp_any(pb, phw);
            int pe = a_set_access(pb, phw, 3) || a_set_format(pb, phw, 2)
                  || a_set_channels(pb, phw, 1) || a_set_rate(pb, phw, 24000, 0)
                  || a_set_ps(pb, phw, 2880, 0) || a_set_periods(pb, phw, 16, 0);
            if (!pe) pe = a_hwp(pb, phw);
            a_hwp_free(phw);
            fprintf(stderr, "probe: play24 rc=%d (%s)\n", pe, pe ? a_strerror(pe) : "ok");
            if (!pe) { a_prepare(pb);
                if (getenv("PROBE_PLAY24_DROP")) { a_drop(pb); fprintf(stderr, "probe: play24 dropped\n"); } }
        }
    }
    snd_pcm_t *pcm = NULL; void *hw = NULL;
    int rc = a_open(&pcm, dev, 1, 0);
    if (rc < 0) { fprintf(stderr, "open: %s\n", a_strerror(rc)); return 1; }
    a_hwp_malloc(&hw); a_hwp_any(pcm, hw);
    a_set_access(pcm, hw, 3); a_set_format(pcm, hw, 2);
    a_set_channels(pcm, hw, 1); a_set_rate(pcm, hw, 16000, 0);
    int per = argc > 3 ? atoi(argv[3]) : 1600;
    a_set_ps(pcm, hw, per, 0); a_set_periods(pcm, hw, 4, 0);
    if ((rc = a_hwp(pcm, hw)) < 0) { fprintf(stderr, "hwp: %s\n", a_strerror(rc)); return 1; }
    a_hwp_free(hw); a_prepare(pcm);
    fprintf(stderr, "probe: open %s ok, reading %ds\n", dev, secs);

    if (getenv("PROBE_KWS") && xr_speech_init(NULL) != 0) {
        fprintf(stderr, "probe: speech init FAIL\n"); return 2;
    }
    if (getenv("PROBE_PLAYER")) {
        extern const xr_audio_output_ops_t *xr_audio_output_ops(void);
        extern int xr_player_init(const xr_audio_output_ops_t *);
        int lvl = getenv("PROBE_PLAYER_LVL") ? atoi(getenv("PROBE_PLAYER_LVL")) : 3;
        if (lvl >= 1) {
            /* L1: 只测 xr_audio_load 的 dlopen 链(不发 ops) */
            extern int xr_audio_load(void);
            if (xr_audio_load() != 0) { fprintf(stderr, "probe: audio_load FAIL\n"); return 2; }
            fprintf(stderr, "probe: audio_load ok (L1)\n");
        }
        if (lvl >= 2) {
            const xr_audio_output_ops_t *ops = xr_audio_output_ops();
            fprintf(stderr, "probe: ops=%p (L2)\n", (void*)ops);
        }
        if (lvl >= 3 && xr_player_init(xr_audio_output_ops()) != 0) {
            fprintf(stderr, "probe: player init FAIL\n"); return 2;
        }
        fprintf(stderr, "probe: player lvl=%d done\n", lvl);
    }
    backend_t *be = NULL;
    if (getenv("PROBE_BACKEND")) {
        be = be_open_local();
        if (!be) { fprintf(stderr, "probe: local backend open FAIL\n"); return 2; }
        if (be_connect(be) != 0) { fprintf(stderr, "probe: local connect FAIL\n"); return 2; }
        fprintf(stderr, "probe: local backend connected\n");
    }
    static short frame[1600];
    int play2_count = 0;
    double t0 = now(); long frames = 0; int cycles = 0;
    while (now() - t0 < secs) {
        if (frames > 96000 * (cycles + 1)) {   /* 每 6s 模拟一次回答收尾: drop+prepare */
            cycles++;
            fprintf(stderr, "probe: drop+prepare #%d\n", cycles);
            a_drop(pcm);
            if (a_prepare(pcm) < 0) { fprintf(stderr, "prepare fail\n"); break; }
        }
        if (getenv("PROBE_STAGING")) {
            /* 原样复刻前端 mic_read 的 vqe staging: 256 读→攒 1600 */
            static int16_t stage[1600 + 256];
            static int fill = 0;
            while (fill < 1600) {
                long r = a_readi(pcm, stage + fill, 256);
                if (r < 0) { a_prepare(pcm); fill = 0; break; }
                fill += (int)r;
            }
            if (fill >= 1600) {
                memcpy(frame, stage, 1600 * 2);
                fill -= 1600;
                memmove(stage, stage + 1600, (size_t)fill * 2);
                frames += 1600;
                if ((frames % 16000) < 1600)
                    fprintf(stderr, "probe: %.1fs frames=%ld\n", now() - t0, frames);
            }
            continue;
        }
        static int play2_done = 0;
        if (getenv("PROBE_PLAY2") && !play2_done && ++play2_count >= 100) {
            play2_done = 1;
if (getenv("PROBE_PLAY2")) {
        static int (*p_open)(snd_pcm_t **, const char *, int, int);
        LOAD(p_open, "snd_pcm_open");
        snd_pcm_t *pb = NULL;
        int rcp = p_open(&pb, "xr_spk", 0, 0);
        fprintf(stderr, "probe: playback xr_spk open rc=%d (%s)\n", rcp, rcp < 0 ? a_strerror(rcp) : "ok");
        if (rcp >= 0) {
            /* 完整 hw_params(复刻 spk_open: 48k/2ch/period 5760) */
            void *phw = NULL;
            a_hwp_malloc(&phw); a_hwp_any(pb, phw);
            int pbrate = getenv("PROBE_PB_RATE") ? atoi(getenv("PROBE_PB_RATE")) : 48000;
            int pbch = getenv("PROBE_PB_CH") ? atoi(getenv("PROBE_PB_CH")) : 2;
            int pe = a_set_access(pb, phw, 3) || a_set_format(pb, phw, 2)
                  || a_set_channels(pb, phw, pbch) || a_set_rate(pb, phw, pbrate, 0)
                  || a_set_ps(pb, phw, 2880, 0) || a_set_periods(pb, phw, 16, 0);
            if (!pe) pe = a_hwp(pb, phw);
            a_hwp_free(phw);
            fprintf(stderr, "probe: playback hw_params rc=%d (%s)\n", pe, pe ? a_strerror(pe) : "ok");
            if (!pe) a_prepare(pb);
        }
    }
        }
        long chunk = getenv("PROBE_CHUNK") ? atoi(getenv("PROBE_CHUNK")) : per;
        long n = a_readi(pcm, frame, chunk);
        if (getenv("PROBE_KWS") && n > 0 && xr_speech_kws_available())
            xr_speech_kws_feed(frame, (int)n);
        if (getenv("PROBE_RECOG")) {
            /* 复刻前端精修: 每 ~3s 用真实音频跑一次 20s RKNN recognize */
            static int rcount = 0;
            if (++rcount >= 30) {   /* 30 帧 ×160 帧/秒(16k/256+staging) ≈ 3s */
                rcount = 0;
                char txt[256];
                int rc = xr_speech_recognize(frame, (size_t)n > 1600 ? 1600 : (size_t)n, txt, sizeof(txt));
                fprintf(stderr, "probe: recognize rc=%d txt=%.12s\n", rc, txt);
            }
        }
        if (be) {   /* 复刻前端: 每 25s ping + 每帧 poll(0) */
            static double last_ping = 0;
            double tnow = now();
            if (tnow - last_ping >= 25.0) {
                last_ping = tnow;
                if (be_ping(be)) fprintf(stderr, "probe: ping fail\n");
                else fprintf(stderr, "probe: ping sent @%.0fs\n", tnow - t0);
            }
            static unsigned char buf[1 << 16];
            char ev_text[256];
            size_t l = 0;
            backend_ev_t ev = be_poll_event(be, buf, sizeof(buf) - 1, &l, ev_text, sizeof(ev_text), 0);
            (void)ev;
        }
        if (n < 0) { fprintf(stderr, "readi err %ld (%s)\n", n, a_strerror((int)n)); a_prepare(pcm); continue; }
        frames += n;
        if ((frames % 16000) < (long)per)
            fprintf(stderr, "probe: %.1fs frames=%ld\n", now() - t0, frames);
    }
    a_drop(pcm); a_close(pcm);
    fprintf(stderr, "probe: DONE %.1fs frames=%ld\n", now() - t0, frames);
    return 0;
}
