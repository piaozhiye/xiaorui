#ifndef XR_AUDIO_CUE_H
#define XR_AUDIO_CUE_H
#include <stddef.h>
#include <stdint.h>

/* Canonical PCM WAV: 24kHz, mono, signed 16-bit, <=10 seconds.
 * Returns 0 on success; caller owns *pcm. Outputs are cleared on failure. */
int xr_cue_load(const char *path, int16_t **pcm, size_t *samples);
#endif
