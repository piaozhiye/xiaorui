#define _POSIX_C_SOURCE 200809L
#include "audio_player.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdatomic.h>
#include <sched.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* 真睡眠: 原来的 sched_yield 忙等校准(20000/ms)在负载下严重失真
 * (2026-11-19 实测 120ms 睡成 1.4s), 会撞上播放线程 300ms 尾巴滞留
 * grace, 把正确行为误报成断言失败。 */
static void sleep_ms(long ms) {
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {}
}

/* ★ 2026-11-19 板端实机断音根因(2): 按 period 对齐写卡。
 *   机制澄清(二次 review 对照内核源码): ALSA 默认 sw 参数
 *   start_threshold=1, 半个 period(1440)的写也能启动流 —— 历史
 *   EPIPE 循环的病灶是零垫子的 60ms 写节奏(供给一抖卡就干转
 *   XRUN), 不是"流起不来"。凑整 period 写 = 每次 writei 给卡
 *   垫进整 120ms 余量。
 *   本测试用假 writei 记录每次写入量, 断言不会写出小于一个
 *   period 的块(eof 尾巴与 grace 排空路径除外)。 */

static int opened, closed, prepared, dropped, inject_underrun;
static size_t written;
static long long checksum;
static atomic_int hold_drain;
static int min_write_seen = 1 << 30;   /* 观察到的最小写入量 */
static unsigned long first_write_count = 0;  /* 首次写入量(ALSA 看到的粒度) */
static int writes;
static unsigned long period_set = 2880;
static atomic_int hold_write;
static atomic_int write_blocked;
static atomic_int capture_next_music_block;
static atomic_int next_music_block_first;
static atomic_int capture_mixer_write_at;
static atomic_int captured_mixer_first;

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
    if (atomic_load(&hold_write)) {
        atomic_store(&write_blocked, 1);
        while (atomic_load(&hold_write)) sleep_ms(1);
    }
    if (inject_underrun) { inject_underrun = 0; return -EPIPE; }
    if (atomic_load(&capture_next_music_block) && writes == 22)
        atomic_store(&next_music_block_first, ((const int16_t *)data)[0]);
    if (writes == atomic_load(&capture_mixer_write_at) && count > 500)
        atomic_store(&captured_mixer_first, ((const int16_t *)data)[500]);
    ++writes;
    if (!first_write_count) first_write_count = count;
    if ((int)count < min_write_seen) min_write_seen = (int)count;
    size_t n = count < 137 ? count : 137; /* force many partial writes */
    const int16_t *pcm = data;
    for (size_t i=0; i < n; ++i) checksum += pcm[i];
    written += n; return (int)n;
}
static int close_fake(snd_pcm_t *p) { (void)p; ++closed; return 0; }
static int drop_fake(snd_pcm_t *p) { (void)p; ++dropped; return 0; }
static const char *error_fake(int e) { (void)e; return "test error"; }
static int delay_fake(snd_pcm_t *p, long *frames) {
    (void)p;
    *frames = 0;
    return 0;
}

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
    sleep_ms(100);                           /* <<300ms grace: 不该写出任何东西 */
    assert(writes == 0);                    /* 半个 period 不许写卡 */
    assert(written == 0);
    xr_player_push_pcm(half, 1440);          /* 补齐第二个 half */
    sleep_ms(200);
    assert(writes >= 1);
    /* 首次写入量凑整 period: 每次 writei 给卡垫进整 120ms 余量
     * (机制澄清见文件头注释 —— 不是"流起不来", 是零垫子怕抖动)。 */
    assert(first_write_count >= period_set);
    xr_player_drop();
}

/* 尾巴滞留: 不满一个 period 的余音在真空 >300ms 后必须被排空写出,
 * 而不是持着等下一轮 —— 否则声卡在 RUNNING 态播干, 下一轮首包凑满
 * period 写入时撞 XRUN(2026-11-19 live10 实测: 问候音 0.45s=3.76period,
 * 尾巴滞留 4.07s 后 EPIPE)。
 * 时序余量说明(二次 review): flush 名义上 300ms 触发, 但 50ms 轮询的
 * 绝对 deadline 在负载下会迟到, 固定 470ms 断言会误报 —— 这里改成
 * 轮询等最多 800ms, 触发即过。 */
static void test_stale_tail_partial_is_flushed_to_prepared(void) {
    written = 0; checksum = 0; writes = 0; min_write_seen = 1 << 30;
    first_write_count = 0;
    assert(xr_player_start() == 0);
    int prep_before = prepared;
    int16_t tail[1440];
    for (size_t i = 0; i < 1440; ++i) tail[i] = (int16_t)(i % 100);
    xr_player_push_pcm(tail, 1440);          /* 半个 period 的尾巴 */
    sleep_ms(100);
    assert(writes == 0);                     /* <<300ms: 凑 period 优先 */
    int flushed = 0;
    for (int i = 0; i < 80 && !flushed; i++) {
        sleep_ms(10);                        /* 真空超 ~300ms: 尾巴滞留触发 */
        if (writes >= 1) flushed = 1;
    }
    assert(flushed);                         /* 尾巴必须已被 grace 排空写出 */
    assert(first_write_count == 1440);       /* 写入量就是那个 partial */
    assert(prepared > prep_before);          /* 且写之前排空+prepare 过 */
    int w_after_tail = writes;
    int16_t next[2880];
    for (size_t i = 0; i < 2880; ++i) next[i] = (int16_t)(i % 100);
    xr_player_push_pcm(next, 2880);          /* 下一轮数据到齐 */
    for (int i = 0; i < 40 && writes == w_after_tail; i++)
        sleep_ms(10);
    assert(writes > w_after_tail);           /* 凑满 period 正常续播 */
    xr_player_drop();
}

/* ★ 2026-11-20 板上磁盘事故(根分区 99% 满): XR_PLAY_DUMP/XR_PLAY_TRACE
 *   都是追加写、无上限 —— play.pcm 长跑到 57MB, 且它们默认落在 /data(根
 *   分区)。规则: 文件达到 *_MAX_MB(默认 32MB)即重置重写, 保留最近的
 *   内容(诊断要的恰恰是最近一段); 打开时已超限同样重置。MAX_MB=0
 *   恢复无上限旧行为。 */
static void test_play_dump_capped(void) {
    const char *path = "/tmp/xr-play-dump-test.pcm";
    unlink(path);
    /* 预置 1MB "历史"内容: 打开时已超 0.2MB 上限, 必须当场重置 */
    FILE *seed = fopen(path, "wb");
    assert(seed);
    static unsigned char junk[65536];
    for (int i = 0; i < 16; ++i)
        assert(fwrite(junk, 1, sizeof(junk), seed) == sizeof(junk));
    fclose(seed);
    setenv("XR_PLAY_DUMP", path, 1);
    setenv("XR_PLAY_DUMP_MAX_MB", "0.2", 1);
    written = 0; checksum = 0; writes = 0;
    assert(xr_player_start() == 0);
    static int16_t pcm[262144];
    for (size_t i = 0; i < 262144; ++i) pcm[i] = (int16_t)(i % 100);
    /* 场景 1: 数据量 << cap, 不触发运行时重置 —— 最终大小就是本轮
     * 写入量(~14KB), 证明预置的 1MB 在打开时已被重置掉 */
    xr_player_push_pcm(pcm, 7200);
    xr_player_finish();
    struct stat st;
    assert(stat(path, &st) == 0);
    assert(st.st_size > 0 && st.st_size < 65536);
    xr_player_drop();
    unsetenv("XR_PLAY_DUMP");
    unsetenv("XR_PLAY_DUMP_MAX_MB");

    /* 场景 2: 0.5MB 数据中途必然跨过 0.2MB cap, 触发运行时重置:
     * 最终 <= cap + 一个 period 的检查余量(2880 样本 × 2B) */
    seed = fopen(path, "wb");
    assert(seed);
    fclose(seed);
    setenv("XR_PLAY_DUMP", path, 1);
    setenv("XR_PLAY_DUMP_MAX_MB", "0.2", 1);
    assert(xr_player_start() == 0);
    xr_player_push_pcm(pcm, 262144);
    xr_player_finish();
    assert(stat(path, &st) == 0);
    long cap = (long)(0.2 * 1024.0 * 1024.0);
    assert(st.st_size > 0);
    assert(st.st_size <= cap + 5760);
    xr_player_drop();
    unsetenv("XR_PLAY_DUMP");
    unsetenv("XR_PLAY_DUMP_MAX_MB");
    unlink(path);
}

/* trace 与 dump 同一套上限规则, 单独走一遍(它走 fprintf 计数路径) */
static void test_play_trace_capped(void) {
    const char *path = "/tmp/xr-play-trace-test.log";
    unlink(path);
    FILE *seed = fopen(path, "wb");
    assert(seed);
    static unsigned char junk[65536];
    for (int i = 0; i < 4; ++i)   /* 预置 256KB: 超过 0.001MB 上限 */
        assert(fwrite(junk, 1, sizeof(junk), seed) == sizeof(junk));
    fclose(seed);
    setenv("XR_PLAY_TRACE", path, 1);
    setenv("XR_PLAY_TRACE_MAX_MB", "0.001", 1);
    written = 0; checksum = 0; writes = 0;
    assert(xr_player_start() == 0);
    static int16_t pcm[262144];   /* 91 个 period ≈ 91 行, 必然跨过 ~35 行上限 */
    for (size_t i = 0; i < 262144; ++i) pcm[i] = (int16_t)(i % 100);
    xr_player_push_pcm(pcm, 262144);
    xr_player_finish();
    struct stat st;
    assert(stat(path, &st) == 0);
    long cap = (long)(0.001 * 1024.0 * 1024.0);
    assert(st.st_size > 0);
    assert(st.st_size <= cap + 512);   /* 重置后只留最近几行 */
    assert(st.st_size < 65536);        /* 预置的 256KB 已被打开时重置掉 */
    xr_player_drop();
    unsetenv("XR_PLAY_TRACE");
    unsetenv("XR_PLAY_TRACE_MAX_MB");
    unlink(path);
}

/* 所有权切换前置: release_device 必须真的 close 句柄(否则对端永远 EBUSY) */
static void test_release_device_closes_handle(void) {
    written = 0; checksum = 0; prepared = 0; inject_underrun = 0;
    int c0 = closed;
    int16_t buf[2880] = {0};
    xr_player_set_music(1);
    assert(xr_player_start() == 0);
    xr_player_push_music_pcm(buf, 1000);
    assert(xr_player_music_queued_samples() == 1000);
    xr_player_push_pcm(buf, 2880);
    sleep_ms(200);
    xr_player_release_device();
    assert(closed > c0);                       /* 真的 close 了 */
    assert(!xr_player_music_active());
    assert(xr_player_music_queued_samples() == 0); /* 不把旧会话残留带到下一次接管 */
    /* 再次 start 会自动重新 open */
    int o0 = opened;
    assert(xr_player_start() == 0);
    assert(opened > o0);
    xr_player_drop();
}

/* ★ 音乐会话在位(Snapcast FIFO -> 播放环): 语音的 finish/drop 不能把音乐收掉。
 *   这是 P1"音乐与语音共用一个播放环"的核心语义, 没有它音乐会被每句语音打断。 */
static void test_music_hold_keeps_playing(void) {
    written = 0; checksum = 0; prepared = 0; inject_underrun = 0;   /* 独立计数 */
    int16_t buf[2880];
    for (size_t i = 0; i < 2880; ++i) buf[i] = (int16_t)(i % 50);

    xr_player_set_music(1);
    assert(xr_player_music_active());
    assert(xr_player_start() == 0);
    for (int i = 0; i < 4; ++i) xr_player_push_music_pcm(buf, 2880);   /* 音乐 */
    sleep_ms(300);
    size_t before = written;

    /* 语音说完: 不许置 eof / join */
    xr_player_finish();
    assert(xr_player_music_active());
    sleep_ms(100);
    /* 线程还活着: 继续推音乐, 还在写卡 */
    for (int i = 0; i < 4; ++i) xr_player_push_music_pcm(buf, 2880);
    sleep_ms(300);
    assert(written > before);

    /* 打断: 只清环余量, 线程仍在 */
    size_t before_drop = written;
    int drops_before = dropped;
    xr_player_drop();
    assert(dropped == drops_before);          /* 音乐占卡时不能 ALSA drop 到 SETUP */
    sleep_ms(100);
    for (int i = 0; i < 4; ++i) xr_player_push_music_pcm(buf, 2880);
    sleep_ms(300);
    assert(written > before_drop);

    /* 音乐源断开超过 idle grace 后会 drain+prepare；重连必须重新预滚。 */
    size_t before_gap = written;
    int prep_before_gap = prepared;
    sleep_ms(450);
    assert(prepared > prep_before_gap);
    xr_player_push_music_pcm(buf, 2880);
    sleep_ms(100);
    assert(written == before_gap);            /* 一周期不足 3-period 预滚量 */
    xr_player_push_music_pcm(buf, 2 * 2880);
    sleep_ms(300);
    assert(written > before_gap);             /* 攒够后重连恢复播放 */

    /* 关掉音乐开关后, finish 应恢复"收干净线程"的原语义 */
    xr_player_set_music(0);
    assert(!xr_player_music_active());
    xr_player_finish();
    assert(xr_player_queued_samples() == 0);
}

/* TTS and Snapcast must be summed in the actual player output, not serialized. */
static void test_music_voice_duck_mix_on_same_output(void) {
    written = 0; checksum = 0; writes = 0; min_write_seen = 1 << 30;
    atomic_store(&hold_write, 1);
    atomic_store(&write_blocked, 0);
    atomic_store(&captured_mixer_first, INT16_MIN);
    atomic_store(&capture_mixer_write_at, -1);
    int16_t music[2 * 2880], voice[2880];
    for (size_t i = 0; i < 2 * 2880; ++i) music[i] = 1000;
    for (size_t i = 0; i < 2880; ++i) voice[i] = 2000;

    xr_player_set_music(1);
    assert(xr_player_start() == 0);
    xr_player_push_music_pcm(music, 2 * 2880); /* 达到 240ms 音乐预滚 */
    for (int i = 0; i < 100 && !atomic_load(&write_blocked); ++i) sleep_ms(10);
    assert(atomic_load(&write_blocked));

    xr_player_voice_begin();
    xr_player_push_music_pcm(music, 2880);
    xr_player_push_pcm(voice, 2880);
    xr_player_set_voice_gain(50);
    atomic_store(&capture_mixer_write_at, writes + 22); /* 下一批 PCM 的首帧 */
    atomic_store(&hold_write, 0);
    for (int i = 0; i < 100 && atomic_load(&captured_mixer_first) == INT16_MIN; ++i)
        sleep_ms(10);
    assert(atomic_load(&captured_mixer_first) == 1250); /* 2000*50% + 1000*25% */

    xr_player_drop();
    xr_player_set_music(0);
    xr_player_set_voice_gain(100);
    xr_player_finish();
}

/* 重联时 Snapclient 可能把缓存 PCM 突发写入 FIFO。总待播量超过 250ms 时，
 * 必须丢弃环里最旧的部分追到实时边沿；否则 B 曾出现 ring 500ms + ALSA 390ms，
 * A 稳态约 250ms 的差距。ALSA mock 暂停第一 period，让生产端先积累可控积压。 */
static void test_music_reconnect_drops_stale_ring_backlog(void) {
    written = 0; checksum = 0; writes = 0; min_write_seen = 1 << 30;
    first_write_count = 0;
    atomic_store(&write_blocked, 0);
    atomic_store(&hold_write, 1);
    atomic_store(&capture_next_music_block, 0);
    atomic_store(&next_music_block_first, -1);
    xr_player_set_music(1);
    assert(xr_player_start() == 0);

    int16_t preroll[2 * 2880];
    for (size_t i = 0; i < 2 * 2880; ++i) preroll[i] = 111;
    xr_player_push_music_pcm(preroll, 2 * 2880);
    for (int i = 0; i < 100 && !atomic_load(&write_blocked); ++i) sleep_ms(10);
    assert(atomic_load(&write_blocked));

    static int16_t backlog[20000];
    for (size_t i = 0; i < 20000; ++i) backlog[i] = (int16_t)(1000 + i);
    xr_player_push_music_pcm(backlog, 20000);
    assert(xr_player_music_queued_samples() >= 20000);
    atomic_store(&capture_next_music_block, 1);
    atomic_store(&hold_write, 0);
    for (int i = 0; i < 200 && atomic_load(&next_music_block_first) < 0; ++i) sleep_ms(10);
    assert(atomic_load(&next_music_block_first) >= 15000); /* 丢掉老队列，保留最新约250ms */

    atomic_store(&capture_next_music_block, 0);
    xr_player_set_music(0);
    xr_player_finish();
    assert(xr_player_queued_samples() == 0);
}

int main(void) {
    const xr_audio_output_ops_t ops = {
        .a_open=open_fake, .a_hwp_malloc=alloc_fake, .a_hwp_any=any_fake,
        .a_set_access=unsigned_fake, .a_set_format=unsigned_fake,
        .a_set_channels=unsigned_fake, .a_set_rate=rate_fake,
        .a_set_ps=period_fake, .a_set_periods=rate_fake,
        .a_hwp=any_fake, .a_hwp_free=free_fake, .a_prepare=prepare_fake,
        .a_writei=write_fake, .a_drop=drop_fake, .a_close=close_fake,
        .a_drain=drain_fake, .a_strerror=error_fake, .a_delay=delay_fake,
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

    /* 新增: 诊断转储文件容量上限(2026-11-20 根分区 99% 事故) */
    test_play_dump_capped();
    test_play_trace_capped();

    /* 新增: 音乐会话在位时, 语音 finish/drop 不能把音乐收掉 */
    test_music_hold_keeps_playing();
    test_music_voice_duck_mix_on_same_output();
    test_music_reconnect_drops_stale_ring_backlog();

    /* 新增: release_device 真的 close 句柄, 再次 start 重新 open */
    test_release_device_closes_handle();

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
