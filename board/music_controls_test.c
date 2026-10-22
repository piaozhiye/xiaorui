#define _POSIX_C_SOURCE 200809L
#include "music_controls.h"
#include <assert.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

static atomic_int feeds, recognitions, trigger;
static const char *transcript = "小瑞小瑞音量小一点";
static void pause_ms(int ms) {
    struct timespec delay = {ms / 1000, (ms % 1000) * 1000000L};
    nanosleep(&delay, NULL);
}
static long read_frame(void *context, int16_t *pcm, size_t n) {
    (void)context;
    memset(pcm, 0, n * sizeof(*pcm));
    pause_ms(1);
    return (long)n;
}
static int kws(const int16_t *pcm, int n) {
    (void)pcm; (void)n;
    atomic_fetch_add(&feeds, 1);
    return atomic_exchange(&trigger, 0);
}
static void reset(void) {}
static int recognize(const int16_t *pcm, size_t n, char *text, size_t cap) {
    (void)pcm;
    assert(n > 16000 && n <= 96000);
    atomic_fetch_add(&recognitions, 1);
    snprintf(text, cap, "%s", transcript);
    return 1;
}
int main(void) {
    xr_music_command_t command;
    assert(!xr_music_command_parse(NULL, &command));
    assert(xr_music_command_parse("音量小一点", &command));
    assert(command.action == XR_MUSIC_VOLUME);
    assert(!xr_music_command_parse("给我讲个故事", &command));
    assert(!xr_music_command_parse("小瑞小瑞大声唱首歌", &command));
    assert(xr_music_command_parse("小瑞小瑞音量小一点", &command));
    assert(command.action == XR_MUSIC_VOLUME && command.volume_op == XR_VOL_DOWN);
    assert(xr_music_command_parse("小瑞小瑞停止播放", &command));
    assert(command.action == XR_MUSIC_STOP);
    /* 实机(2026-10-15): 预滚环+捕获窗把唤醒词收了两遍, 剥一次不够会漏停 */
    assert(xr_music_command_parse("小瑞小瑞小瑞小瑞停止播放", &command));
    assert(command.action == XR_MUSIC_STOP);
    assert(xr_music_command_parse("小瑞小瑞停下", &command));
    assert(xr_music_command_parse("小瑞小瑞别说了", &command));
    assert(command.action == XR_MUSIC_STOP);
    assert(xr_music_command_parse("小瑞小瑞闭嘴", &command));
    assert(command.action == XR_MUSIC_STOP);
    assert(command.action == XR_MUSIC_STOP);
    /* 声学门兜底: 唤醒音节掉出预滚窗, 转写整句无唤醒词 */
    assert(xr_music_command_parse("声音小一点", &command));
    assert(command.action == XR_MUSIC_VOLUME);
    assert(!xr_music_command_parse("小瑞小瑞", &command));          /* 纯唤醒无指令 */
    assert(!xr_music_command_parse("小瑞小瑞讲个睡前故事", &command));
    xr_music_control_ops_t ops = {read_frame, kws, reset, recognize};
    assert(xr_music_controls_start(NULL, NULL, 1000) < 0);
    assert(xr_music_controls_start(&ops, NULL, 999) < 0);
    assert(xr_music_controls_start(&ops, NULL, 1000) == 0);
    pause_ms(30);
    assert(atomic_load(&feeds) > 0 && atomic_load(&recognitions) == 0);
    atomic_store(&trigger, 1);
    int received = 0;
    for (int i = 0; i < 500 && !received; ++i) {
        pause_ms(1);
        received = xr_music_controls_poll(&command);
    }
    assert(received && command.action == XR_MUSIC_VOLUME);
    assert(!xr_music_controls_poll(&command));
    xr_music_controls_stop();
    xr_music_controls_stop();
    assert(atomic_load(&recognitions) == 1);
    /* Exercise the monitor's general-speech path, not only the strict parser. */
    const char *cases[] = {"小瑞小瑞", "小瑞", "小瑞小瑞小瑞小瑞", "小瑞小瑞开灯"};
    for (size_t k = 0; k < sizeof(cases) / sizeof(cases[0]); ++k) {
        transcript = cases[k];
        int before = atomic_load(&recognitions);
        assert(xr_music_controls_start(&ops, NULL, 1000) == 0);
        atomic_store(&trigger, 1);
        received = 0;
        for (int i = 0; i < 500; ++i) {
            pause_ms(1);
            if (xr_music_controls_poll(&command)) { received = 1; break; }
            if (atomic_load(&recognitions) > before && !xr_music_controls_capturing()) break;
        }
        assert(atomic_load(&recognitions) == before + 1);
        if (k < 3) assert(!received && !xr_music_controls_poll(&command));
        else assert(received && command.action == XR_MUSIC_FORWARD && !strcmp(command.text, "开灯"));
        xr_music_controls_stop();
    }
    puts("music controls PASS");
    return 0;
}
