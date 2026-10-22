#ifndef XR_AUDIO_PLAYER_H
#define XR_AUDIO_PLAYER_H
#include <stddef.h>
#include <stdint.h>
typedef struct _snd_pcm snd_pcm_t;
/* Loaded ALSA operations, supplied once before the playback thread starts. */
typedef struct {
    int (*a_open)(snd_pcm_t **, const char *, int, int);
    int (*a_hwp_malloc)(void **);
    int (*a_hwp_any)(snd_pcm_t *, void *);
    int (*a_set_access)(snd_pcm_t *, void *, unsigned);
    int (*a_set_format)(snd_pcm_t *, void *, unsigned);
    int (*a_set_channels)(snd_pcm_t *, void *, unsigned);
    int (*a_set_rate)(snd_pcm_t *, void *, unsigned, int);
    int (*a_set_ps)(snd_pcm_t *, void *, unsigned long, int);
    int (*a_set_periods)(snd_pcm_t *, void *, unsigned, int);
    int (*a_hwp)(snd_pcm_t *, void *);
    void (*a_hwp_free)(void *);
    int (*a_prepare)(snd_pcm_t *);
    int (*a_writei)(snd_pcm_t *, const void *, unsigned long);
    int (*a_drop)(snd_pcm_t *);
    int (*a_close)(snd_pcm_t *);
    int (*a_drain)(snd_pcm_t *);
    const char *(*a_strerror)(int);
} xr_audio_output_ops_t;

int xr_player_init(const xr_audio_output_ops_t *ops);
int xr_player_start(void);
void xr_player_finish(void);
void xr_player_drop(void);
void xr_player_push_pcm(const int16_t *pcm, size_t samples);
int xr_player_push_opus(const unsigned char *packet, size_t size);
size_t xr_player_queued_samples(void);
/* 软件音量(1-100; -1/0=直通)。线程安全, 下一批 writei(≤120ms)生效。 */
void xr_player_set_volume(int pct);
int xr_player_volume(void);
#endif
