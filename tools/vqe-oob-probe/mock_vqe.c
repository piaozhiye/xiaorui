/* Deterministic probe validation; never links or invokes vendor code. */
#include <stdlib.h>
#include <string.h>

static int mode(const char *name) {
    const char *value = getenv("MOCK_VQE_MODE");
    return value && !strcmp(value, name);
}
void *rkaudio_preprocess_init_by_conf(int rate, int bits, int src, int ref, char *path) {
    (void)rate; (void)bits; (void)src; (void)ref; (void)path;
    return mode("init-fail") ? NULL : (void *)1;
}
int rkaudio_out_chan_invoke(void *st) {
    (void)st;
    return mode("bad-channels") ? 0 : mode("two-channels") ? 2 : 1;
}
int rkaudio_preprocess_short(void *st, short *in, short *out, int n, int *wake) {
    (void)st; (void)in; (void)wake;
    if (n != 3072 || mode("process-fail")) return -1;
    int samples = 768 * (mode("two-channels") ? 2 : 1);
    for (int i = 0; i < samples; i++) out[i] = 123;
    if (mode("overrun")) out[samples + 64] = 0;
    if (mode("underrun")) out[-1] = 0;
    return mode("bad-length") ? samples * 2 + 2 : samples * 2;
}
void rkaudio_preprocess_destory(void *st) { (void)st; }
