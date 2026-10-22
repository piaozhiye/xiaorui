#include "audio_capture.h"
#include <assert.h>
#include <dirent.h>
#include <stdint.h>
#include <stdio.h>

static int fd_count(void) {
    DIR *dir = opendir("/proc/self/fd");
    assert(dir);
    int count = 0;
    struct dirent *entry;
    while ((entry = readdir(dir)))
        if (entry->d_name[0] != '.') ++count;
    closedir(dir);
    return count;
}

static void one_capture(void) {
    snd_pcm_t *capture = xr_capture_open("null");
    assert(capture);
    int16_t pcm[1600];
    assert(xr_capture_read(capture, pcm, 1600) == 1600);
    assert(xr_capture_drop(capture) == 0);
    assert(xr_capture_prepare(capture) == 0);
    assert(xr_capture_read(capture, pcm, 1600) == 1600);
    assert(xr_capture_close(capture) == 0);
}

int main(void) {
    assert(xr_audio_load() == 0 && xr_audio_load() == 0);
    assert(xr_audio_output_ops());
    one_capture(); /* Warm ALSA's shared configuration before checking descriptors. */
    int before = fd_count();
    for (int i = 0; i < 5; ++i) one_capture();
    assert(xr_capture_open("xr_deliberately_missing_test_pcm") == NULL);
    assert(fd_count() == before);
    puts("capture null PCM/read/drop/prepare/reopen/fd tests passed");
    return 0;
}
