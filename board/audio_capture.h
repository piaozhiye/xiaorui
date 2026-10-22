#ifndef XR_AUDIO_CAPTURE_H
#define XR_AUDIO_CAPTURE_H
#include "audio_player.h"

int xr_audio_load(void);
const xr_audio_output_ops_t *xr_audio_output_ops(void);
snd_pcm_t *xr_capture_open(const char *device);
snd_pcm_t *xr_capture_open_nch(const char *device, int channels, int rate,
                               unsigned long period);
long xr_capture_read(snd_pcm_t *capture, void *pcm, unsigned long frames);
int xr_capture_prepare(snd_pcm_t *capture);
int xr_capture_drop(snd_pcm_t *capture);
int xr_capture_close(snd_pcm_t *capture);
const char *xr_audio_error(int error);
#endif
