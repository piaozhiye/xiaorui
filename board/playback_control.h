#ifndef XR_PLAYBACK_CONTROL_H
#define XR_PLAYBACK_CONTROL_H
#include "music_controls.h"

typedef struct { int music; int ducked; } xr_playback_control_t;
/* Main loop is the only caller. Stop/join precedes any ALSA/KWS/ASR handover. */
void xr_playback_control_tick(xr_playback_control_t *state, int volume);
void xr_playback_control_stop(xr_playback_control_t *state, int volume);
int xr_playback_control_poll(xr_music_command_t *command);
#endif
