#include "playback_control.h"
#include <assert.h>
#include <stdio.h>
static int running, capturing, volume, pending;
int xr_music_controls_running(void) { return running; }
int xr_music_controls_capturing(void) { return capturing; }
void xr_music_controls_stop(void) { running = capturing = 0; }
int xr_music_controls_poll(xr_music_command_t *command) {
    command->action = XR_MUSIC_STOP;
    return pending;
}
void xr_player_set_volume(int pct) { volume = pct; }
int main(void) {
    xr_playback_control_t state = {0};
    xr_music_command_t command;
    running = pending = 1;
    assert(xr_playback_control_poll(&command)); /* ordinary answer, not music */
    assert(command.action == XR_MUSIC_STOP);
    state.music = capturing = 1;
    xr_playback_control_tick(&state, 88);
    assert(volume == XR_VOLUME_MIN && state.ducked);
    xr_playback_control_stop(&state, 88);
    assert(volume == 88 && !state.ducked && !state.music && !running);
    assert(!xr_playback_control_poll(&command));
    xr_playback_control_stop(&state, 88);
    puts("playback control ownership/duck/ordinary answer PASS");
    return 0;
}
