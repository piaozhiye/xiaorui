/* P3 让位测试(无需声卡): 覆盖环状态和直占声卡的完整交接调用链。
 * 由 test-audio-player.sh 编译:
 *   audio_player.c + xr_volume.c + audio_player_yield_test.c
 * 让位是回合级: 前端进 RESPOND 调 voice_begin, 离开 RESPOND 调 voice_end。
 */
#define _POSIX_C_SOURCE 200809L
#include "audio_player.h"
#include "audio_mix.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int16_t buf[8000];
static char temp_dir[] = "/tmp/xr-yield-test-XXXXXX";
static char log_path[256], released_path[256], active_path[256], pid_path[256], status_path[256];
static int opened, closed, prepared;

static void log_line(const char *line) {
    FILE *f = fopen(log_path, "a");
    assert(f);
    fprintf(f, "%s\n", line);
    fclose(f);
}
static int fake_open(snd_pcm_t **pcm, const char *name, int stream, int flags) {
    (void)stream; (void)flags;
    assert(strcmp(name, "xr_spk") == 0);
    if (getenv("XR_TEST_FORCE_BUSY")) return -EBUSY;
    if (access(released_path, F_OK) != 0) return -EBUSY;
    *pcm = (snd_pcm_t *)&opened;
    opened++;
    log_line("open");
    return 0;
}
static int fake_alloc(void **p) { *p = &opened; return 0; }
static int fake_ok(snd_pcm_t *p, void *v) { (void)p; (void)v; return 0; }
static int fake_unsigned(snd_pcm_t *p, void *v, unsigned n) {
    (void)n; return fake_ok(p, v);
}
static int fake_rate(snd_pcm_t *p, void *v, unsigned n, int d) {
    (void)d; return fake_unsigned(p, v, n);
}
static int fake_period(snd_pcm_t *p, void *v, unsigned long n, int d) {
    (void)d; return fake_unsigned(p, v, (unsigned)n);
}
static void fake_free(void *p) { (void)p; }
static int fake_prepare(snd_pcm_t *p) { (void)p; prepared++; return 0; }
static int fake_write(snd_pcm_t *p, const void *pcm, unsigned long n) {
    (void)p; (void)pcm; return (int)n;
}
static int fake_drop(snd_pcm_t *p) { (void)p; return 0; }
static int fake_drain(snd_pcm_t *p) { (void)p; return 0; }
static int fake_close(snd_pcm_t *p) {
    (void)p; closed++; log_line("close"); return 0;
}
static const char *fake_error(int e) { (void)e; return "test error"; }

static void write_script(const char *name, const char *body) {
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", temp_dir, name);
    FILE *f = fopen(path, "w");
    assert(f);
    assert(fputs(body, f) >= 0);
    assert(fclose(f) == 0);
    assert(chmod(path, 0700) == 0);
}

static void test_direct_device_yield_and_restore(void) {
    assert(mkdtemp(temp_dir));
    snprintf(log_path, sizeof(log_path), "%s/events", temp_dir);
    snprintf(released_path, sizeof(released_path), "%s/released", temp_dir);
    snprintf(active_path, sizeof(active_path), "%s/snapclient-active", temp_dir);
    snprintf(pid_path, sizeof(pid_path), "%s/snapclient-pid", temp_dir);
    snprintf(status_path, sizeof(status_path), "%s/alsa-status", temp_dir);
    char fake_client_path[512];
    char client_log_path[512];
    snprintf(fake_client_path, sizeof(fake_client_path), "%s/fake-snapclient", temp_dir);
    snprintf(client_log_path, sizeof(client_log_path), "%s/client.log", temp_dir);
    char path_env[512];
    snprintf(path_env, sizeof(path_env), "%s:/usr/bin:/bin", temp_dir);
    assert(setenv("PATH", path_env, 1) == 0);
    assert(setenv("XR_MUSIC_YIELD", "1", 1) == 0);
    const char *resume_script = getenv("XR_TEST_REAL_RESUME_SCRIPT");
    assert(resume_script && *resume_script);
    assert(setenv("XR_SNAPCLIENT_RESTART", "restart-snapclient", 1) == 0);
    assert(setenv("XR_SNAPCLIENT_BIN", fake_client_path, 1) == 0);
    assert(setenv("XR_SNAPCAST_SERVER", "192.0.2.10:1704", 1) == 0);
    assert(setenv("XR_SNAPCLIENT_ALSA_STATUS", status_path, 1) == 0);
    assert(setenv("XR_SNAPCLIENT_LOG", client_log_path, 1) == 0);
    assert(setenv("XR_SNAPCLIENT_READY_TIMEOUT_MS", "300", 1) == 0);
    assert(setenv("XR_TEST_ACTIVE", active_path, 1) == 0);
    assert(setenv("XR_TEST_CLIENT_PID", pid_path, 1) == 0);
    assert(setenv("XR_TEST_ALSA_STATUS", status_path, 1) == 0);
    assert(setenv("XR_TEST_REAL_RESUME_SCRIPT", resume_script, 1) == 0);
    unsetenv("XR_PA_AUTO_SUSPEND");

    write_script("pgrep", "#!/bin/sh\necho pgrep >> \"$XR_YIELD_LOG\"\n[ -e \"$XR_TEST_ACTIVE\" ]\n");
    write_script("pkill", "#!/bin/sh\necho pkill >> \"$XR_YIELD_LOG\"\n[ ! -f \"$XR_TEST_CLIENT_PID\" ] || kill \"$(cat \"$XR_TEST_CLIENT_PID\")\" 2>/dev/null || true\nrm -f \"$XR_TEST_ACTIVE\" \"$XR_TEST_ALSA_STATUS\"\ntouch \"$XR_YIELD_RELEASED\"\n");
    write_script("pactl", "#!/bin/sh\nif [ \"$1\" = get-default-sink ]; then echo test-sink; else echo \"pactl $*\" >> \"$XR_YIELD_LOG\"; fi\n");
    write_script("restart-snapclient", "#!/bin/sh\nexec \"$XR_TEST_REAL_RESUME_SCRIPT\"\n");
    write_script("fake-snapclient", "#!/bin/sh\necho restart >> \"$XR_YIELD_LOG\"\ntouch \"$XR_TEST_ACTIVE\"\necho $$ > \"$XR_TEST_CLIENT_PID\"\necho 'state: RUNNING' > \"$XR_TEST_ALSA_STATUS\"\nexec sleep 30\n");
    FILE *active = fopen(active_path, "w"); assert(active); fclose(active);
    assert(setenv("XR_YIELD_LOG", log_path, 1) == 0);
    assert(setenv("XR_YIELD_RELEASED", released_path, 1) == 0);

    const xr_audio_output_ops_t ops = {
        .a_open=fake_open, .a_hwp_malloc=fake_alloc, .a_hwp_any=fake_ok,
        .a_set_access=fake_unsigned, .a_set_format=fake_unsigned,
        .a_set_channels=fake_unsigned, .a_set_rate=fake_rate,
        .a_set_ps=fake_period, .a_set_periods=fake_rate,
        .a_hwp=fake_ok, .a_hwp_free=fake_free, .a_prepare=fake_prepare,
        .a_writei=fake_write, .a_drop=fake_drop, .a_close=fake_close,
        .a_drain=fake_drain, .a_strerror=fake_error,
    };
    assert(xr_player_init(&ops) == 0);

    /* start 必须先停客户端并挂起 PA，释放标记出现后才能打开硬件。 */
    assert(xr_player_start() == 0);
    assert(opened == 1 && prepared > 0);
    int16_t audio[2880] = {0};
    xr_player_push_pcm(audio, 2880);
    xr_player_finish();
    assert(closed == 1); /* 恢复音乐前，前端句柄已真正 close */

    FILE *f = fopen(log_path, "r");
    assert(f);
    char contents[1024] = {0};
    size_t n = fread(contents, 1, sizeof(contents) - 1, f);
    (void)n;
    fclose(f);
    char *kill = strstr(contents, "pkill\n");
    char *probe = strstr(contents, "pgrep\n");
    char *suspend = strstr(contents, "pactl suspend-sink test-sink 1\n");
    char *open = strstr(contents, "open\n");
    char *close = strstr(contents, "close\n");
    char *resume = strstr(contents, "pactl suspend-sink test-sink 0\n");
    char *restart = strstr(contents, "restart\n");
    assert(probe && kill && suspend && open && close && resume && restart);
    assert(probe < kill && kill < suspend && suspend < open && open < close
           && close < resume && resume < restart);

    /* drop(取消/断线) 同样必须 close、恢复 sink、重启客户端。 */
    assert(unlink(released_path) == 0);
    f = fopen(log_path, "w"); assert(f); fclose(f);
    int closed_before = closed;
    assert(xr_player_start() == 0);
    xr_player_drop();
    assert(closed > closed_before);
    f = fopen(log_path, "r"); assert(f);
    memset(contents, 0, sizeof(contents));
    n = fread(contents, 1, sizeof(contents) - 1, f);
    (void)n;
    fclose(f);
    assert(strstr(contents, "pactl suspend-sink test-sink 0\n"));
    assert(strstr(contents, "restart\n"));

    /* 持久 PA idle 配置启用后，yield 仍停/启客户端，但跳过每轮 pactl。 */
    assert(unlink(released_path) == 0);
    f = fopen(log_path, "w"); assert(f); fclose(f);
    assert(setenv("XR_PA_AUTO_SUSPEND", "1", 1) == 0);
    assert(xr_player_start() == 0);
    xr_player_finish();
    f = fopen(log_path, "r"); assert(f);
    memset(contents, 0, sizeof(contents));
    n = fread(contents, 1, sizeof(contents) - 1, f);
    (void)n;
    fclose(f);
    assert(strstr(contents, "pkill\n"));
    assert(strstr(contents, "open\n"));
    assert(strstr(contents, "close\n"));
    assert(strstr(contents, "restart\n"));
    assert(!strstr(contents, "pactl "));
    unsetenv("XR_PA_AUTO_SUSPEND");

    /* 开卡超时也必须恢复 PA 并重启客户端，不能遗留已让位状态。 */
    assert(unlink(released_path) == 0);
    f = fopen(log_path, "w"); assert(f); fclose(f);
    assert(setenv("XR_TEST_FORCE_BUSY", "1", 1) == 0);
    assert(xr_player_start() == -1);
    unsetenv("XR_TEST_FORCE_BUSY");
    f = fopen(log_path, "r"); assert(f);
    memset(contents, 0, sizeof(contents));
    n = fread(contents, 1, sizeof(contents) - 1, f);
    (void)n;
    fclose(f);
    assert(strstr(contents, "pactl suspend-sink test-sink 1\n"));
    assert(strstr(contents, "pactl suspend-sink test-sink 0\n"));
    assert(strstr(contents, "restart\n"));
    assert(!strstr(contents, "open\n"));

    /* 数值 0 必须是关闭态；不应探测/终止客户端或挂起 PA。 */
    assert(access(released_path, F_OK) == 0);
    f = fopen(log_path, "w"); assert(f); fclose(f);
    assert(setenv("XR_MUSIC_YIELD", "0", 1) == 0);
    assert(xr_player_start() == 0);
    xr_player_drop();
    xr_player_release_device();
    unsetenv("XR_MUSIC_YIELD");
    f = fopen(log_path, "r"); assert(f);
    memset(contents, 0, sizeof(contents));
    n = fread(contents, 1, sizeof(contents) - 1, f);
    (void)n;
    fclose(f);
    assert(strstr(contents, "open\n"));
    assert(strstr(contents, "close\n"));
    assert(!strstr(contents, "pgrep\n"));
    assert(!strstr(contents, "pkill\n"));
    assert(!strstr(contents, "pactl "));

    int cleanup_rc = system("pkill -x snapclient >/dev/null 2>&1");
    (void)cleanup_rc;
    unlink(log_path); unlink(released_path); unlink(active_path); unlink(pid_path); unlink(status_path);
    char path[512];
    snprintf(path, sizeof(path), "%s/pgrep", temp_dir); unlink(path);
    snprintf(path, sizeof(path), "%s/pkill", temp_dir); unlink(path);
    snprintf(path, sizeof(path), "%s/pactl", temp_dir); unlink(path);
    snprintf(path, sizeof(path), "%s/restart-snapclient", temp_dir); unlink(path);
    snprintf(path, sizeof(path), "%s/fake-snapclient", temp_dir); unlink(path);
    snprintf(path, sizeof(path), "%s/client.log", temp_dir); unlink(path);
    assert(rmdir(temp_dir) == 0);
    puts("direct-device yield/restore tests passed");
}

int main(void) {
    int16_t mixed[4];
    const int16_t music_samples[] = {1000, -1000, 30000, -30000};
    const int16_t voice_samples[] = {2000, -2000, 10000, -10000};
    xr_audio_gain_ramp_t gain;
    xr_audio_gain_ramp_init(&gain, 100);
    xr_audio_mix_s16_ramped(mixed, music_samples, voice_samples, 4, 25, 0, &gain);
    assert(mixed[0] == 2250 && mixed[1] == -2250);
    assert(mixed[2] == 17500 && mixed[3] == -17500);
    xr_audio_mix_s16_ramped(mixed, music_samples, voice_samples, 4, 100, 0, &gain);
    assert(mixed[0] == 3000 && mixed[1] == -3000);
    const int16_t hot_music[] = {32767, -32768};
    const int16_t hot_voice[] = {20000, -20000};
    xr_audio_mix_s16_ramped(mixed, hot_music, hot_voice, 2, 100, 0, &gain);
    assert(mixed[0] == INT16_MAX && mixed[1] == INT16_MIN);

    /* Duck/restore transitions reach target over 20ms without a sample jump. */
    int16_t music_tone[480], silence[480] = {0}, ramped[480];
    for (size_t i = 0; i < 480; ++i) music_tone[i] = 20000;
    xr_audio_gain_ramp_init(&gain, 100);
    xr_audio_mix_s16_ramped(ramped, music_tone, silence, 480, 25, 480, &gain);
    assert(ramped[0] < 20000 && ramped[0] > 19000);
    assert(ramped[479] == 5000 && gain.current_q16 == gain.target_q16);
    for (size_t i = 1; i < 480; ++i)
        assert(ramped[i - 1] >= ramped[i]);
    xr_audio_mix_s16_ramped(ramped, music_tone, silence, 480, 100, 480, &gain);
    assert(ramped[0] > 5000 && ramped[0] < 5100);
    assert(ramped[479] == 20000 && gain.current_q16 == gain.target_q16);
    for (size_t i = 1; i < 480; ++i)
        assert(ramped[i - 1] <= ramped[i]);

    test_direct_device_yield_and_restore();
    memset(buf, 0, sizeof(buf));

    /* 无音乐会话时: voice_begin/end 都无动作(不改让位态) */
    xr_player_voice_begin();
    assert(!xr_player_music_ducked());
    xr_player_voice_end();
    assert(!xr_player_music_ducked());

    /* 音乐会话在位 */
    xr_player_set_music(1);
    assert(xr_player_music_active());
    assert(!xr_player_music_ducked());

    /* 音乐入环 */
    xr_player_push_music_pcm(buf, 1000);
    assert(xr_player_music_queued_samples() == 1000);

    /* 语音开始: 只 duck 音乐；两路都保留并可进入混音。 */
    xr_player_voice_begin();
    assert(xr_player_music_ducked());
    assert(xr_player_queued_samples() == 0);
    assert(xr_player_music_queued_samples() == 1000);

    /* 语音入环走通用 push_pcm, 不再自动让位(已验证) */
    xr_player_push_pcm(buf, 500);
    assert(xr_player_queued_samples() == 500);

    /* 语音期间音乐仍然推进，不能停 Snapcast 时间线。 */
    xr_player_push_music_pcm(buf, 800);
    assert(xr_player_queued_samples() == 500);
    assert(xr_player_music_queued_samples() == 1800);

    /* 语音结束: 清未排空的语音尾巴，音乐队列不断档并恢复原增益。 */
    xr_player_voice_end();
    assert(!xr_player_music_ducked());
    assert(xr_player_queued_samples() == 0);
    assert(xr_player_music_queued_samples() == 1800);

    /* 恢复后音乐重新入环 */
    xr_player_push_music_pcm(buf, 700);
    assert(xr_player_music_queued_samples() == 2500);

    /* voice_end 幂等: 未暂停时不清环 */
    xr_player_voice_end();
    assert(xr_player_music_queued_samples() == 2500);

    /* voice_begin 幂等 */
    xr_player_voice_begin();
    assert(xr_player_music_ducked());
    assert(xr_player_music_queued_samples() == 2500);
    xr_player_voice_begin();
    assert(xr_player_music_ducked());

    /* 结束音乐会话: 清让位态 */
    xr_player_set_music(0);
    assert(!xr_player_music_active());
    assert(!xr_player_music_ducked());
    assert(xr_player_music_queued_samples() == 0);

    printf("yield state machine tests passed\n");
    return 0;
}
