#include "audio_player.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

static int opened, closed, prepared, inject_underrun;
static size_t written;
static long long checksum;
static int open_fake(snd_pcm_t **p, const char *name, int stream, int flags) {
    (void)stream; (void)flags; assert(!strcmp(name,"xr_spk"));
    *p = (snd_pcm_t *)&opened; ++opened; return 0;
}
static int alloc_fake(void **p) { *p = &opened; return 0; }
static int any_fake(snd_pcm_t *p, void *hw) { (void)p; (void)hw; return 0; }
static int unsigned_fake(snd_pcm_t *p, void *hw, unsigned value) {
    (void)p; (void)hw; (void)value; return 0;
}
static int rate_fake(snd_pcm_t *p, void *hw, unsigned value, int direction) {
    (void)direction; return unsigned_fake(p,hw,value);
}
static int period_fake(snd_pcm_t *p, void *hw, unsigned long value, int direction) {
    (void)direction; return unsigned_fake(p,hw,(unsigned)value);
}
static void free_fake(void *p) { (void)p; }
static int prepare_fake(snd_pcm_t *p) { (void)p; ++prepared; return 0; }
static int write_fake(snd_pcm_t *p, const void *data, unsigned long count) {
    (void)p;
    if (inject_underrun) { inject_underrun = 0; return -EPIPE; }
    size_t n = count < 137 ? count : 137; /* force many partial writes */
    const int16_t *pcm = data;
    for (size_t i = 0; i < n; ++i) checksum += pcm[i];
    written += n; return (int)n;
}
static int close_fake(snd_pcm_t *p) { (void)p; ++closed; return 0; }
static int no_op(snd_pcm_t *p) { (void)p; return 0; }
static const char *error_fake(int e) { (void)e; return "test error"; }

int main(void) {
    const xr_audio_output_ops_t ops = {
        .a_open=open_fake, .a_hwp_malloc=alloc_fake, .a_hwp_any=any_fake,
        .a_set_access=unsigned_fake, .a_set_format=unsigned_fake,
        .a_set_channels=unsigned_fake, .a_set_rate=rate_fake,
        .a_set_ps=period_fake, .a_set_periods=rate_fake,
        .a_hwp=any_fake, .a_hwp_free=free_fake, .a_prepare=prepare_fake,
        .a_writei=write_fake, .a_drop=no_op, .a_close=close_fake,
        .a_drain=no_op, .a_strerror=error_fake,
    };
    assert(xr_player_init(&ops)==0);
    int16_t pcm[7200];
    long long expected = 0;
    for (size_t i=0; i<7200; ++i) { pcm[i]=(int16_t)(i % 100); expected += pcm[i]; }
    inject_underrun = 1;
    assert(xr_player_start()==0);
    xr_player_push_pcm(pcm,7200);
    xr_player_finish();
    assert(written==7200 && checksum==expected && prepared>=2);
    assert(xr_player_queued_samples()==0);   /* spk 常驻后句柄不按轮关闭 */
    assert(xr_player_start()==0);
    xr_player_drop(); /* sleeping consumer must wake and join */
    assert(opened >= 1);
    assert(xr_player_start()==0);
    xr_player_push_pcm(pcm,7200);
    xr_player_finish();
    assert(written==14400 && checksum==2*expected);
    xr_player_drop();
    puts("audio player queue/partial write/underrun/restart tests passed");
    return 0;
}
