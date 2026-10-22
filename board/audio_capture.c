#define _POSIX_C_SOURCE 200809L
#include "audio_capture.h"
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
/* ---------- ALSA via dlopen (abi 组合与 xz_capture 相同, 已验证) ---------- */
typedef struct _snd_pcm snd_pcm_t;
typedef long snd_pcm_sframes_t;
static void *alsa_lib;
static int (*a_writei)(snd_pcm_t *, const void *, unsigned long);
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
static snd_pcm_sframes_t (*a_readi)(snd_pcm_t *, void *, unsigned long);
static int (*a_drop)(snd_pcm_t *);
static int (*a_close)(snd_pcm_t *);
static int (*a_drain)(snd_pcm_t *);
static const char *(*a_strerror)(int);
#define A_CAPTURE 1
#define A_S16 2
#define A_RW 3

int xr_audio_load(void) {
    if (alsa_lib) return 0;
    alsa_lib = dlopen("libasound.so.2", RTLD_NOW);
    if (!alsa_lib) { fprintf(stderr, "[xr] dlopen libasound: %s\n", dlerror()); return -1; }
    struct { const char *n; void **p; } t[] = {
        {"snd_pcm_open", (void**)&a_open},
        {"snd_pcm_hw_params_malloc", (void**)&a_hwp_malloc},
        {"snd_pcm_hw_params_any", (void**)&a_hwp_any},
        {"snd_pcm_hw_params_set_access", (void**)&a_set_access},
        {"snd_pcm_hw_params_set_format", (void**)&a_set_format},
        {"snd_pcm_hw_params_set_channels", (void**)&a_set_channels},
        {"snd_pcm_hw_params_set_rate", (void**)&a_set_rate},
        {"snd_pcm_hw_params_set_period_size", (void**)&a_set_ps},
        {"snd_pcm_hw_params_set_periods", (void**)&a_set_periods},
        {"snd_pcm_hw_params", (void**)&a_hwp},
        {"snd_pcm_hw_params_free", (void**)&a_hwp_free},
        {"snd_pcm_prepare", (void**)&a_prepare},
        {"snd_pcm_readi", (void**)&a_readi},
        {"snd_pcm_drop", (void**)&a_drop},
        {"snd_pcm_close", (void**)&a_close},
        {"snd_pcm_drain", (void**)&a_drain},
        {"snd_pcm_writei", (void**)&a_writei},
        {"snd_strerror", (void**)&a_strerror},
    };
    for (size_t i = 0; i < sizeof(t)/sizeof(t[0]); i++) {
        *t[i].p = dlsym(alsa_lib, t[i].n);
        if (!*t[i].p) {
            fprintf(stderr, "[xr] dlsym %s: %s\n", t[i].n, dlerror());
            dlclose(alsa_lib); alsa_lib = NULL; return -1;
        }
    }
    return 0;
}

const xr_audio_output_ops_t *xr_audio_output_ops(void) {
    static xr_audio_output_ops_t ops;
    if (!alsa_lib) return NULL;
    ops = (xr_audio_output_ops_t) {
        .a_open = a_open,
        .a_hwp_malloc = a_hwp_malloc,
        .a_hwp_any = a_hwp_any,
        .a_set_access = a_set_access,
        .a_set_format = a_set_format,
        .a_set_channels = a_set_channels,
        .a_set_rate = a_set_rate,
        .a_set_ps = a_set_ps,
        .a_set_periods = a_set_periods,
        .a_hwp = a_hwp,
        .a_hwp_free = a_hwp_free,
        .a_prepare = a_prepare,
        .a_writei = a_writei,
        .a_drop = a_drop,
        .a_close = a_close,
        .a_drain = a_drain,
        .a_strerror = a_strerror,
    };
    return &ops;
}

snd_pcm_t *xr_capture_open(const char *device) {
    return xr_capture_open_nch(device, 1, 16000, 1600);
}

snd_pcm_t *xr_capture_open_nch(const char *device, int channels, int rate,
                               unsigned long period) {
    if (!alsa_lib) return NULL;
    snd_pcm_t *capture = NULL;
    void *hw = NULL;
#define STEP(name, call) do { int rc_ = (call); if (rc_ < 0) { \
    fprintf(stderr, "[xr] capture_nch %s: %s\n", name, a_strerror(rc_)); rc = rc_; goto fail; } } while (0)
    int rc = 0;
    STEP("open", a_open(&capture, device, A_CAPTURE, 0));
    STEP("hwp_malloc", a_hwp_malloc(&hw));
    STEP("hwp_any", a_hwp_any(capture, hw));
    STEP("access", a_set_access(capture, hw, A_RW));
    STEP("format", a_set_format(capture, hw, A_S16));
    STEP("channels", a_set_channels(capture, hw, channels));
    STEP("rate", a_set_rate(capture, hw, rate, 0));
    STEP("period", a_set_ps(capture, hw, period, 0));
    STEP("periods", a_set_periods(capture, hw, 4, 0));
    STEP("hwp", a_hwp(capture, hw));
#undef STEP
    a_hwp_free(hw); hw = NULL;
    rc = a_prepare(capture);
    if (rc < 0) goto fail;
    fprintf(stderr, "[xr] capture %s: %dch/%dHz period=%lu (实开)\n",
            device, channels, rate, period);
    return capture;
fail:
    fprintf(stderr, "[xr] capture %s(%dch/%dHz): %s\n", device, channels, rate, a_strerror(rc));
    if (hw) a_hwp_free(hw);
    if (capture) a_close(capture);
    return NULL;
}

long xr_capture_read(snd_pcm_t *p, void *pcm, unsigned long frames) {
    return a_readi(p, pcm, frames);
}
int xr_capture_prepare(snd_pcm_t *p) { return a_prepare(p); }
int xr_capture_drop(snd_pcm_t *p) { return a_drop(p); }
int xr_capture_close(snd_pcm_t *p) { return a_close(p); }
const char *xr_audio_error(int error) { return a_strerror(error); }
