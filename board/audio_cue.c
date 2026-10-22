#include "audio_cue.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t le32(const unsigned char *p) {
    return p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static unsigned le16(const unsigned char *p) {
    return p[0] | ((unsigned)p[1] << 8);
}

int xr_cue_load(const char *path, int16_t **pcm, size_t *samples) {
    if (!pcm || !samples) return -EINVAL;
    *pcm = NULL; *samples = 0;
    if (!path) return -EINVAL;
    FILE *file = fopen(path, "rb");
    /* 不能裸返 -errno: 本函数 0 表示成功, 若 fopen 失败而 errno 恰为 0,
       调用方会把"空音频"当成加载成功(日志还打 loaded (0 samples)),
       提示音静默失效且无从察觉。 */
    if (!file) return errno ? -errno : -EINVAL;
    unsigned char header[44];
    int result = -EINVAL;
    int16_t *buffer = NULL;
    if (fread(header, 1, sizeof(header), file) != sizeof(header)) goto done;
    uint32_t bytes = le32(header + 40);
    if (memcmp(header, "RIFF", 4) || memcmp(header + 8, "WAVEfmt ", 8) ||
        le32(header + 16) != 16 || le16(header + 20) != 1 ||
        le16(header + 22) != 1 || le32(header + 24) != 24000 ||
        le32(header + 28) != 48000 || le16(header + 32) != 2 ||
        le16(header + 34) != 16 || memcmp(header + 36, "data", 4) ||
        bytes < 200 || bytes > 480000 || bytes % 2 || le32(header + 4) < bytes + 36)
        goto done;
    buffer = malloc(bytes);
    if (!buffer) { result = -ENOMEM; goto done; }
    if (fread(buffer, 1, bytes, file) != bytes) goto done;
    *pcm = buffer; *samples = bytes / sizeof(int16_t);
    buffer = NULL;
    result = 0;
done:
    free(buffer);
    fclose(file);
    return result;
}
