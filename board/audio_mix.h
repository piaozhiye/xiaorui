#ifndef XR_AUDIO_MIX_H
#define XR_AUDIO_MIX_H

#include <stddef.h>
#include <stdint.h>

/* Music gain is ramped over a short interval so duck/restore does not click. */
typedef struct {
    int32_t current_q16;
    int32_t target_q16;
    unsigned remaining;
} xr_audio_gain_ramp_t;

#define XR_AUDIO_GAIN_ONE_Q16 65536

static inline void xr_audio_gain_ramp_init(xr_audio_gain_ramp_t *ramp,
                                           int gain_percent) {
    if (gain_percent < 0) gain_percent = 0;
    if (gain_percent > 100) gain_percent = 100;
    int32_t gain = (int32_t)((int64_t)gain_percent * XR_AUDIO_GAIN_ONE_Q16 / 100);
    ramp->current_q16 = gain;
    ramp->target_q16 = gain;
    ramp->remaining = 0;
}

/* Saturating mono PCM mixer. Duck gain applies only to music. */
static inline void xr_audio_mix_s16_ramped(int16_t *out, const int16_t *music,
                                           const int16_t *voice, size_t samples,
                                           int music_gain_percent,
                                           unsigned ramp_samples,
                                           xr_audio_gain_ramp_t *ramp) {
    if (music_gain_percent < 0) music_gain_percent = 0;
    if (music_gain_percent > 100) music_gain_percent = 100;
    int32_t target = (int32_t)((int64_t)music_gain_percent * XR_AUDIO_GAIN_ONE_Q16 / 100);
    if (target != ramp->target_q16) {
        ramp->target_q16 = target;
        ramp->remaining = ramp_samples;
        if (!ramp->remaining) ramp->current_q16 = target;
    }
    for (size_t i = 0; i < samples; ++i) {
        if (ramp->remaining) {
            int32_t delta = ramp->target_q16 - ramp->current_q16;
            int32_t step = delta / (int32_t)ramp->remaining;
            if (!step && delta) step = delta > 0 ? 1 : -1;
            ramp->current_q16 += step;
            if (--ramp->remaining == 0) ramp->current_q16 = ramp->target_q16;
        }
        int32_t m = music ? (int32_t)(((int64_t)music[i] * ramp->current_q16) / XR_AUDIO_GAIN_ONE_Q16) : 0;
        int32_t v = voice ? voice[i] : 0;
        int32_t mixed = m + v;
        if (mixed > INT16_MAX) mixed = INT16_MAX;
        if (mixed < INT16_MIN) mixed = INT16_MIN;
        out[i] = (int16_t)mixed;
    }
}

#endif
