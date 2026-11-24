/* VQE memory probe: measure rkaudio_vqe algorithm footprint for N mic / M ref.
 *
 * Purpose: answer "how much DDR does rkaudio_vqe use" without touching the
 * sound card and without reading vendor source. It dlopen()s the board's
 * librkaudio_vqe.so, initialises the algorithm with a given channel
 * geometry, feeds synthetic frames, then reports the memory delta.
 *
 * Why dlopen instead of linking: the board's .so is built against the board
 * glibc; the host cross toolchain may be older. dlopen defers every symbol
 * resolution to the board, so the probe always matches what ships.
 *
 * Metrics (all from /proc/self/status, so they are the real DDR footprint):
 *   anon_delta  heap growth caused by the algorithm init  <- scales with mic count
 *   file_delta  resident code/data pages of librkaudio_*.so <- fixed cost
 *   hwm         peak RSS of the whole process
 *
 * Usage:
 *   mem_probe CONFIG [src_chan] [ref_chan] [frames] [rate]
 * Example (4 mic + 1 ref, 48 kHz):
 *   mem_probe config_4mic1ref.json 5 1 600 48000
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void *(*init_fn)(int, int, int, int, char *);
typedef int (*chan_fn)(void *);
typedef int (*short_fn)(void *, short *, short *, int, int *);
typedef void (*destroy_fn)(void *);

struct mem {
    long rss, anon, file, hwm;
};

static long field(const char *text, const char *key) {
    const char *p = strstr(text, key);
    if (!p) return -1;
    return strtol(p + strlen(key), NULL, 10);
}

static void read_mem(struct mem *m) {
    FILE *f = fopen("/proc/self/status", "r");
    char line[256];
    if (!f) { memset(m, 0, sizeof(*m)); return; }
    m->rss = m->anon = m->file = m->hwm = 0;
    while (fgets(line, sizeof(line), f)) {
        long v = field(line, "VmRSS:");
        if (v >= 0) { m->rss = v; continue; }
        v = field(line, "RssAnon:");
        if (v >= 0) { m->anon = v; continue; }
        v = field(line, "RssFile:");
        if (v >= 0) { m->file = v; continue; }
        v = field(line, "VmHWM:");
        if (v >= 0) { m->hwm = v; continue; }
    }
    fclose(f);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: mem_probe CONFIG [src_chan] [ref_chan] [frames] [rate]\n");
        return 2;
    }
    const char *lib = argc > 6 ? argv[6] : "/usr/lib/librkaudio_vqe.so";
    int src_chan = argc > 2 ? atoi(argv[2]) : 2;
    int ref_chan = argc > 3 ? atoi(argv[3]) : 1;
    long frames = argc > 4 ? atol(argv[4]) : 600;
    int rate = argc > 5 ? atoi(argv[5]) : 48000;
    if (src_chan < 1 || ref_chan < 1 || frames < 1) {
        fprintf(stderr, "bad arguments\n");
        return 2;
    }

    struct mem before, after, peak;
    read_mem(&before);

    void *h = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    if (!h) { fprintf(stderr, "RESULT dlopen-failed: %s\n", dlerror()); return 2; }
    init_fn init = (init_fn)dlsym(h, "rkaudio_preprocess_init_by_conf");
    chan_fn out_chan = (chan_fn)dlsym(h, "rkaudio_out_chan_invoke");
    short_fn process = (short_fn)dlsym(h, "rkaudio_preprocess_short");
    destroy_fn destroy = (destroy_fn)dlsym(h, "rkaudio_preprocess_destory");
    if (!init || !out_chan || !process) {
        fprintf(stderr, "RESULT missing-symbol\n");
        return 2;
    }
    read_mem(&after);   /* library mapped, algorithm not yet initialised */

    void *st = init(rate, 16, src_chan, ref_chan, argv[1]);
    if (!st) { fprintf(stderr, "RESULT init-failed\n"); return 2; }
    int ch = out_chan(st);
    read_mem(&peak);

    /* Feed frames so any lazily-allocated per-frame state is touched.
     * The plugin expects in_size = frame * (src_chan + ref_chan): the
     * reference channels are interleaved into the same input block.
     * The VQE algorithm always works on 16 ms frames, so the frame length
     * follows the sample rate: 48 kHz -> 768, 16 kHz -> 256. */
    const int FRAME = rate * 16 / 1000;
    int total_chan = src_chan + ref_chan;
    size_t in_n = (size_t)FRAME * total_chan;
    short *input = calloc(in_n, sizeof(short));
    short *out = calloc((size_t)FRAME * (ch > 0 ? ch : 1), sizeof(short));
    if (!input || !out) { fprintf(stderr, "RESULT alloc-failed\n"); return 2; }
    long t = 0;
    for (long f = 0; f < frames; f++) {
        for (int i = 0; i < FRAME; i++, t++) {
            double env = 0.5 * (1.0 + (t % 4800) / 4800.0);
            for (int c = 0; c < total_chan; c++) {
                input[i * total_chan + c] = (short)(4000.0 * env *
                    (0.5 + 0.5 * ((t + c * 977) % 997) / 997.0));
            }
        }
        int wake = 0;
        int rc = process(st, input, out, (int)in_n, &wake);
        if (rc < 0) { fprintf(stderr, "RESULT process-failed rc=%d frame=%ld\n", rc, f); return 2; }
    }
    read_mem(&peak);
    if (destroy) destroy(st);
    free(input);
    free(out);

    printf("RESULT src_chan=%d ref_chan=%d out_chan=%d frames=%ld "
           "anon_delta_kb=%ld file_delta_kb=%ld rss_delta_kb=%ld hwm_kb=%ld "
           "anon_total_kb=%ld file_total_kb=%ld\n",
           src_chan, ref_chan, ch, frames,
           peak.anon - after.anon, peak.file - after.file,
           peak.rss - after.rss, peak.hwm,
           peak.anon, peak.file);
    fflush(stdout);
    return 0;
}
