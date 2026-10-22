/* Client lifecycle only: id is not a server-echoed protocol correlation id. */
#ifndef XR_BACKEND_TURN_H
#define XR_BACKEND_TURN_H
#include <stdint.h>

typedef enum {
    XR_TURN_IDLE, XR_TURN_SUBMITTED, XR_TURN_ACCEPTED,
    XR_TURN_PLAYING, XR_TURN_FINISHED, XR_TURN_FAILED
} xr_turn_state_t;

typedef struct {
    uint64_t id;
    xr_turn_state_t state;
    unsigned long audio_packets;
} xr_turn_t;

static inline int xr_turn_active(const xr_turn_t *t) {
    return t->state == XR_TURN_SUBMITTED || t->state == XR_TURN_ACCEPTED
        || t->state == XR_TURN_PLAYING;
}
static inline void xr_turn_reset(xr_turn_t *t) {
    t->state = XR_TURN_IDLE;
    t->audio_packets = 0;
}
static inline void xr_turn_begin(xr_turn_t *t) {
    if (++t->id == 0) ++t->id;
    t->state = XR_TURN_SUBMITTED;
    t->audio_packets = 0;
}
static inline int xr_turn_accepted(const xr_turn_t *t) {
    return t->state == XR_TURN_ACCEPTED || t->state == XR_TURN_PLAYING;
}
static inline int xr_turn_accept(xr_turn_t *t) {
    if (!xr_turn_active(t)) return 0;
    if (t->state == XR_TURN_SUBMITTED) t->state = XR_TURN_ACCEPTED;
    return 1;
}
static inline void xr_turn_audio(xr_turn_t *t) {
    ++t->audio_packets;
    t->state = XR_TURN_PLAYING;
}
static inline int xr_turn_has_audio(const xr_turn_t *t) {
    return t->audio_packets > 0 && t->state != XR_TURN_IDLE
        && t->state != XR_TURN_FINISHED;
}
#endif
