/* Isolated VQE probe: query actual channels, check return bytes and guards.
 * No ALSA. guard-page mode detects even uninstrumented writes at output end.
 * Run with core dumps disabled; never use a production process as the probe.
 */
#define _GNU_SOURCE
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

extern void *rkaudio_preprocess_init_by_conf(int, int, int, int, char *);
extern int rkaudio_out_chan_invoke(void *);
extern int rkaudio_preprocess_short(void *, short *, short *, int, int *);
extern void rkaudio_preprocess_destory(void *);
enum { FRAME = 768, IN_CH = 4, GUARD = 1024, MAGIC = 0x5a5a };

static int dirty(const short *p, int n) {
    for (int i = 0; i < n; i++) if (p[i] != MAGIC) return i;
    return -1;
}
static void fill(short *p, int n) {
    for (int i = 0; i < n; i++) p[i] = MAGIC;
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: probe CONFIG [frames] [canary|guard-page]\n"); return 2; }
    char *end = NULL;
    long frames = argc > 2 ? strtol(argv[2], &end, 10) : 6000;
    if (frames < 1 || frames > 100000 || (argc > 2 && *end)) return 2;
    int page_mode = argc > 3 && !strcmp(argv[3], "guard-page");
    if (argc > 3 && !page_mode && strcmp(argv[3], "canary")) return 2;
    void *st = rkaudio_preprocess_init_by_conf(48000, 16, 2, 2, argv[1]);
    if (!st) { fprintf(stderr, "RESULT init-failed\n"); return 2; }
    int ch = rkaudio_out_chan_invoke(st);
    if (ch < 1 || ch > IN_CH) { fprintf(stderr, "RESULT invalid-output-channels=%d\n", ch); return 2; }
    int in_n = FRAME * IN_CH, out_n = FRAME * ch;
    size_t bytes = (size_t)out_n * sizeof(short);
    printf("geometry rate=48000 input_samples=%d output_channels=%d output_samples=%d output_bytes=%zu mode=%s\n",
           in_n, ch, out_n, bytes, page_mode ? "guard-page" : "canary");
    fflush(stdout);
    short *input = calloc((size_t)in_n + 2 * GUARD, sizeof(short));
    short *allocation = NULL, *out = NULL;
    void *mapping = MAP_FAILED;
    size_t mapping_n = 0;
    if (page_mode) {
        long page = sysconf(_SC_PAGESIZE);
        if (page <= 0) return 2;
        size_t usable = (bytes + (size_t)page - 1) / (size_t)page * (size_t)page;
        mapping_n = usable + (size_t)page;
        mapping = mmap(NULL, mapping_n, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (mapping == MAP_FAILED || mprotect((char *)mapping + usable, (size_t)page, PROT_NONE)) return 2;
        out = (short *)((char *)mapping + usable - bytes);
    } else {
        allocation = calloc((size_t)out_n + 2 * GUARD, sizeof(short));
        if (allocation) out = allocation + GUARD;
    }
    if (!input || !out) return 2;
    fill(input, GUARD); fill(input + GUARD + in_n, GUARD);
    if (!page_mode) { fill(allocation, GUARD); fill(out + out_n, GUARD); }
    int result = 0, wake = 0, rc = 0;
    long t = 0, completed = 0;
    for (long f = 0; f < frames; f++) {
        for (int i = 0; i < FRAME; i++, t++) {
            double env = 0.5 * (1 + sin(2 * 3.141592653589793 * 3.0 * t / 48000.0));
            double far = 6000 * env * sin(2 * 3.141592653589793 * (400 + 1200 * (t % 48000) / 48000.0) * t / 48000.0);
            double near = 5000 * sin(2 * 3.141592653589793 * 220.0 * t / 48000.0);
            short *in = input + GUARD;
            in[i*4] = (short)(near + 0.5 * far);
            in[i*4+1] = (short)(0.9 * near + 0.4 * far);
            in[i*4+2] = (short)far; in[i*4+3] = (short)(0.95 * far);
        }
        rc = rkaudio_preprocess_short(st, input + GUARD, out, in_n, &wake);
        if (f == 0) { printf("first_return_bytes=%d\n", rc); fflush(stdout); }
        int pre = page_mode ? -1 : dirty(allocation, GUARD);
        int post = page_mode ? -1 : dirty(out + out_n, GUARD);
        int ipre = dirty(input, GUARD), ipost = dirty(input + GUARD + in_n, GUARD);
        if (pre >= 0 || post >= 0 || ipre >= 0 || ipost >= 0) {
            fprintf(stderr, "RESULT guard-dirty frame=%ld out_pre=%d out_post=%d in_pre=%d in_post=%d returned_bytes=%d\n",
                    f, pre, post, ipre, ipost, rc);
            result = 1; break;
        }
        if (rc != (int)bytes) {
            fprintf(stderr, "RESULT output-length-mismatch frame=%ld returned=%d expected=%zu\n", f, rc, bytes);
            result = 1; break;
        }
        completed++;
    }
    printf("RESULT %s completed=%ld requested=%ld\n", result ? "FAIL" : "PASS", completed, frames);
    fflush(stdout);
    if (result) return result; /* preserve first finding; don't enter destroy */
    rkaudio_preprocess_destory(st);
    free(input); free(allocation);
    if (mapping != MAP_FAILED) munmap(mapping, mapping_n);
    return 0;
}
