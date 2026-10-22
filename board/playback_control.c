#include <stdio.h>
#include "playback_control.h"
#include "audio_player.h"

void xr_playback_control_tick(xr_playback_control_t *state, int volume) {
    int duck = state->music && xr_music_controls_capturing();
    if (duck != state->ducked) {
        fprintf(stderr, "[music] duck %s (music=%d capturing=%d)\n",
                duck ? "on->10%" : "off->restore", state->music,
                xr_music_controls_capturing());
        xr_player_set_volume(duck ? XR_VOLUME_MIN : volume);
        state->ducked = duck;
    }
}

void xr_playback_control_stop(xr_playback_control_t *state, int volume) {
    xr_music_controls_stop();
    if (state->ducked) xr_player_set_volume(volume);
    state->ducked = 0;
    state->music = 0;
}

int xr_playback_control_poll(xr_music_command_t *command) {
    /* Ordinary replies also own a control thread; media=music only governs ducking. */
    return xr_music_controls_running() && xr_music_controls_poll(command);
}
