#define _POSIX_C_SOURCE 200809L
#include "audio_player.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdatomic.h>
#include <sched.h>
#include <string.h>
#include <time.h>

/* 真睡眠: 原来的 sched_yield 忙等校准(20000/ms)在负载下严重失真
 * (2026-11-19 实测 120ms 睡成 1.4s), 会撞上播放线程 300ms 尾巴滞留
 * grace, 把正确行为误报成断言失败。 */
static void sleep_ms(long ms) {
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {}
}

/* ★ 2026-11-19 板端实机断音根因(2):
 *   网关改 1 倍速下发后, 板端环里数据来得慢, 播放线程常只凑到 1440 样本
 *   (半个 period), 写进 period=2880 的 ALSA 流 -> EPIPE -> prepare 重来,
 *   陷入 EPIPE 循环, 声卡状态停在 SETUP 不消费, 音频被反复丢弃。
 *   ALSA 要求: 写入量必须 >= 一个 period, 否则流无法启动。
 *   本测试用假 writei 记录每次写入量, 断言不会写出小于一个 period 的块。 */

static int opened, closed, prepared, inject_underrun;
static size_t written;
static long long checksum;
static atomic_int hold_drain;
static int min_write_seen = 1 << 30;   /* 观察到的最小写入量 */
static unsigned long first_write_count = 0;  /* 首次写入量(ALSA 看到的粒度) */
static int writes;
static unsigned long period_set = 2880;

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
    (void)direction; period_set = value; return unsigned_fake(p,hw,(unsigned)value);
}
static void free_fake(void *p) { (void)p; }
static int prepare_fake(snd_pcm_t *p) { (void)p; ++prepared; return 0; }
static int write_fake(snd_pcm_t *p, const void *data, unsigned long count) {
    (void)p;
    if (inject_underrun) { inject_underrun = 0; return -EPIPE; }
    ++writes;
    if (!first_write_count) first_write_count = count;
    if ((int)count < min_write_seen) min_write_seen = (int)count;
    size_t n = count < 137 ? count : 137; /* force many partial writes */
    const int16_t *pcm = data;
    for (size_t i=0; i < n; ++i) checksum += pcm[i];
    written += n; return (int)n;
}
static int close_fake(snd_pcm_t *p) { (void)p; ++closed; return 0; }
static int no_op(snd_pcm_t *p) { (void)p; return 0; }
static const char *error_fake(int e) { (void)e; return "test error"; }

static int drain_fake(snd_pcm_t *pcm) {
    (void)pcm;
    while (atomic_load(&hold_drain)) sched_yield();
    return 0;
}

static int cancel_poll(void *context) {
    int *calls = context;
    (*calls)++;
    atomic_store(&hold_drain, 0);
    return 1;
}

/* 一次只推半个 period: 播放线程必须等够一个 period 才写卡。 */
static void test_half_period_is_not_written(void) {
    written = 0; checksum = 0; writes = 0; min_write_seen = 1 << 30;
    first_write_count = 0;
    assert(xr_player_start() == 0);
    int16_t half[1440];
    for (size_t i = 0; i < 1440; ++i) half[i] = (int16_t)(i % 100);
    xr_player_push_pcm(half, 1440);          /* 只有半个 period */
    sleep_ms(120);                            /* 给播放线程时间: 不该写出任何东西 */
    assert(writes == 0);                    /* 半个 period 不许写卡 */
    assert(written == 0);
    xr_player_push_pcm(half, 1440);          /* 补齐第二个 half */
    sleep_ms(200);
    assert(writes >= 1);
    /* 首次写入量必须 >= 一个 period: ALSA 收不到一个完整 period 就起不来流。 */
    assert(first_write_count >= period_set);
    xr_player_drop();
}

/* 尾巴滞留: 不满一个 period 的余音在真空 >300ms 后必须写进未启动的流,
 * 而不是持着等下一轮 —— 否则声卡在 RUNNING 态播干, 下一轮首包凑满
 * period 写入时撞 XRUN(2026-11-19 live10 实测: 问候音 0.45s=3.76period,
 * 尾巴滞留 4.07s 后 EPIPE)。 */
static void test_stale_tail_partial_is_flushed_to_prepared(void) {
    written = 0; checksum = 0; writes = 0; min_write_seen = 1 << 30;
    first_write_count = 0;
    assert(xr_player_start() == 0);
    int prep_before = prepared;
    int16_t tail[1440];
    for (size_t i = 0; i < 1440; ++i) tail[i] = (int16_t)(i % 100);
    xr_player_push_pcm(tail, 1440);          /* 半个 period 的尾巴 */
    sleep_ms(120);
    assert(writes == 0);                     /* 300ms 内: 凑 period 优先 */
    sleep_ms(350);                           /* 真空超 300ms: 尾巴滞留 */
    assert(writes >= 1);                     /* 尾巴必须已写进未启动的流 */
    assert(first_write_count == 1440);       /* 写入量就是那个 partial */
    assert(prepared > prep_before);          /* 且写之前排空+prepare 过 */
    int w_after_tail = writes;
    int16_t next[2880];
    for (size_t i = 0; i < 2880; ++i) next[i] = (int16_t)(i % 100);
    xr_player_push_pcm(next, 2880);          /* 下一轮数据到齐 */
    sleep_ms(200);
    assert(writes > w_after_tail);           /* 凑满 period 正常续播 */
    xr_player_drop();
}

int main(void) {
    const xr_audio_output_ops_t ops = {
        .a_open=open_fake, .a_hwp_malloc=alloc_fake, .a_hwp_any=any_fake,
        .a_set_access=unsigned_fake, .a_set_format=unsigned_fake,
        .a_set_channels=unsigned_fake, .a_set_rate=rate_fake,
        .a_set_ps=period_fake, .a_set_periods=rate_fake,
        .a_hwp=any_fake, .a_hwp_free=free_fake, .a_prepare=prepare_fake,
        .a_writei=write_fake, .a_drop=no_op, .a_close=close_fake,
        .a_drain=drain_fake, .a_strerror=error_fake,
    };
    assert(xr_player_init(&ops)==0);

    /* 原有用例: 一次推 7200 样本(2.5 个 period) */
    int16_t pcm[7200];
    long long expected = 0;
    for (size_t i=0; i<7200; ++i) { pcm[i]=(int16_t)(i % 100); expected += pcm[i]; }
    inject_underrun = 1;
    assert(xr_player_start()==0);
    xr_player_push_pcm(pcm,7200);
    xr_player_finish();
    assert(written==7200 && checksum==expected && prepared>=2);
    assert(xr_player_queued_samples()==0);
    assert(xr_player_start()==0);
    xr_player_drop();
    assert(xr_player_start()==0);
    xr_player_push_pcm(pcm,7200);
    xr_player_finish();
    assert(written==14400 && checksum==2*expected);
    xr_player_drop();

    /* 新增: 半个 period 不许写卡 */
    test_half_period_is_not_written();

    /* 新增: 尾巴滞留 >300ms 必须排空+写进未启动的流 */
    test_stale_tail_partial_is_flushed_to_prepared();

    atomic_store(&hold_drain, 1);
    assert(xr_player_start() == 0);
    int calls = 0;
    int cancelled = xr_player_finish_with_poll(cancel_poll, &calls);
    assert(cancelled == 1 && calls == 1);
    assert(xr_player_start() == 0);
    xr_player_drop();
    puts("audio player period-alignment tests passed");
    return 0;
}
