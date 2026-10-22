#ifndef XR_SPEECH_ROUTE_H
#define XR_SPEECH_ROUTE_H

typedef enum {
    XR_SPEECH_LOCAL,
    XR_SPEECH_UPLOAD,
    XR_SPEECH_NO_UPLINK,
    XR_SPEECH_ECHO_DROP
} xr_speech_route_t;

/* Pure capability policy. Legacy routing stays unchanged with local ASR on.
 * With it off, guard-period VAD alone is suppressed; explicit KWS may re-wake,
 * but the server must still confirm wake=required outside a follow-up window. */
static inline xr_speech_route_t xr_speech_route(int local_asr, int uplink,
                                               int kws_hit, int echo_guarded) {
    if (local_asr)
        return uplink && !kws_hit && !echo_guarded ? XR_SPEECH_UPLOAD : XR_SPEECH_LOCAL;
    if (!uplink) return XR_SPEECH_NO_UPLINK;
    if (echo_guarded && !kws_hit) return XR_SPEECH_ECHO_DROP;
    return XR_SPEECH_UPLOAD;
}
#endif
