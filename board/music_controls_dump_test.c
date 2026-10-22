#define _POSIX_C_SOURCE 200809L
#include "music_controls.h"
#include <assert.h>
#include <errno.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static const char *mode;
static unsigned opens, flushes;
static atomic_int feeds;

FILE *__wrap_fopen(const char *path, const char *flags) {
    ++opens;
    assert(!strcmp(path, "/test-dump/ctrl-raw.pcm"));
    assert(!strcmp(flags, "wb"));
    if (!strcmp(mode, "missing")) { errno = ENOENT; return NULL; }
    return tmpfile();  /* No named files, particularly not in /. */
}
int __real_fflush(FILE *stream);
int __wrap_fflush(FILE *stream) {
    ++flushes;
    if (!strcmp(mode, "write-fail")) { errno = ENOSPC; return EOF; }
    return __real_fflush(stream);
}
static long read_frame(void *context, int16_t *pcm, size_t n) {
    (void)context;
    memset(pcm, 0, n * sizeof(*pcm));
    struct timespec pause = {0, 1000000}; nanosleep(&pause, NULL);
    return (long)n;
}
static int feed(const int16_t *pcm, int n) {
    (void)pcm; (void)n; atomic_fetch_add(&feeds, 1); return 0;
}
static void reset(void) {}
static int recognize(const int16_t *pcm, size_t n, char *text, size_t cap) {
    (void)pcm; (void)n; (void)text; (void)cap; assert(0); return 0;
}
int main(int argc, char **argv) {
    assert(argc == 2); mode = argv[1];
    if (!strcmp(mode, "unset")) unsetenv("XR_UTT_DUMP");
    else setenv("XR_UTT_DUMP", !strcmp(mode, "empty") ? "" : "/test-dump", 1);
    xr_music_control_ops_t ops = {read_frame, feed, reset, recognize};
    for (int round = 0; round < 2; ++round) {
        atomic_store(&feeds, 0);
        assert(!xr_music_controls_start(&ops, NULL, 1000));
        for (int i = 0; i < 1000 && atomic_load(&feeds) < 30; ++i) {
            struct timespec pause = {0, 1000000}; nanosleep(&pause, NULL);
        }
        xr_music_controls_stop();
        assert(atomic_load(&feeds) >= 30);  /* Debug failure must not stop KWS. */
        if (!strcmp(mode, "empty") || !strcmp(mode, "unset")) assert(opens == 0);
        else assert(opens == (unsigned)(round + 1));
        if (!strcmp(mode, "missing")) assert(flushes == 0);
        if (!strcmp(mode, "write-fail")) assert(flushes == (unsigned)(round + 1));
    }
    puts("music controls dump failure isolation PASS");
    return 0;
}
