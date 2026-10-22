/* End-to-end gateway test without opening microphones or speakers. */
#define _POSIX_C_SOURCE 200809L
#include "backend.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

static unsigned le16(const unsigned char *p) { return p[0] | ((unsigned)p[1] << 8); }
static unsigned le32(const unsigned char *p) { return le16(p) | (le16(p + 2) << 16); }
static int16_t *read_wav(const char *path, size_t *samples) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    unsigned char header[16]; int format_ok = 0;
    int16_t *pcm = NULL;
    if (fread(header, 1, 12, f) != 12 || memcmp(header, "RIFF", 4)
        || memcmp(header + 8, "WAVE", 4)) goto done;
    while (fread(header, 1, 8, f) == 8) {
        unsigned bytes = le32(header + 4);
        if (bytes > 16000u * 2 * 15) goto done;
        if (!memcmp(header, "fmt ", 4) && bytes >= 16) {
            if (fread(header, 1, 16, f) != 16) goto done;
            format_ok = le16(header) == 1 && le16(header + 2) == 1
                && le32(header + 4) == 16000 && le16(header + 14) == 16;
            if (fseek(f, bytes - 16 + (bytes & 1), SEEK_CUR)) goto done;
        } else if (!memcmp(header, "data", 4) && format_ok && bytes && !(bytes & 1)) {
            pcm = malloc(bytes);
            if (!pcm || fread(pcm, 1, bytes, f) != bytes) { free(pcm); pcm = NULL; }
            else *samples = bytes / 2;
            break;
        } else if (fseek(f, bytes + (bytes & 1), SEEK_CUR)) goto done;
    }
done:
    fclose(f); return pcm;
}
int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc != 2) return 2;
    size_t n = 0; int16_t *pcm = read_wav(argv[1], &n);
    if (!pcm) { fprintf(stderr, "need PCM16/mono/16k WAV <=15s\n"); return 2; }
    backend_t *b = be_open_cloud();
    backend_audio_request_t r = {.pcm=pcm, .samples=n};
    if (!b || be_connect(b) < 0 || be_submit_audio(b, &r) < 0) { free(pcm); return 2; }
    free(pcm);
    int audio = 0, accepted = 0, stopped = 0;
    double deadline = now_s() + 60.0;
    while (now_s() < deadline) {
        unsigned char buf[65536]; char text[1024]; size_t len = 0;
        backend_ev_t ev = be_poll_event(b, buf, sizeof(buf), &len, text, sizeof(text), 100);
        if (ev == EV_STT) { accepted = 1; printf("gateway ASR: %s\n", text); }
        if (ev == EV_TTS_AUDIO) ++audio;
        if (ev == EV_TTS_STOP) { stopped = 1; break; }
        if (ev == EV_ERROR) break;
    }
    be_disconnect(b);
    printf("accepted=%d audio_packets=%d stopped=%d\n", accepted, audio, stopped);
    return accepted && audio && stopped ? 0 : 1;
}
