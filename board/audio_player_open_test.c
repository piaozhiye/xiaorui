/* audio_player_open_test.c — spk_open 失败路径的门禁。
 *
 * 背景: audio_player.c 里 hw_params 失败那条路径明确 close + spk=NULL
 * (注释写"泄漏句柄会让后续全 busy"), 但紧随其后的 a_prepare 失败路径
 * 直接 return -1、没有关句柄。init 里"SAI 瞬态重试 3 次"还会把这个泄漏
 * 放大 3 倍。后果是一次 prepare 失败就留下僵尸句柄, 采集侧随后
 * Device or resource busy, 播放永久不可用。
 *
 * 这里用假 ops 表驱动失败路径, 断言:
 *   A. 冷态(prepare 失败) -> 每个 open 都必须 close, 且事后能恢复
 *   B. 冷态(hw_params 失败) -> 每个 open 都必须 close, 且事后能恢复
 *   C. 冷态(open 失败)     -> 没有句柄可关, 不能凭空多关
 *   D. 复用分支             -> prepare 失败必须让 spk_open 返回失败, 不能吞掉
 *
 * 顺序敏感: spk 是 audio_player.c 里的静态常驻句柄, 一旦打开就再也回不到
 * 冷态, 所以"冷态"用例必须排在前面。
 */
#include "audio_player.h"
#include <stdio.h>
#include <string.h>

static int failures;
#define CHECK(cond, msg) do { \
    if (!(cond)) { failures++; fprintf(stderr, "FAIL %d: %s\n", __LINE__, (msg)); } \
} while (0)

static int opened, closed, prepared, open_attempts;
static int fail_hwp, fail_open;
/* prepare 失败按次数消耗(而不是一次性标志): A 段要"init 重试的每一次都失败",
   才能断言"每次 open 都配一次 close"; D 段只要一次。 */
static int fail_prepare_n;
static char handles[16];

static int open_fake(snd_pcm_t **p, const char *name, int stream, int flags) {
    (void)stream; (void)flags;
    if (!strcmp(name, "xr_spk") && fail_open) { fail_open = 0; return -2; }
    ++open_attempts;
    *p = (snd_pcm_t *)&handles[opened % 16];
    ++opened;
    return 0;
}
static int alloc_fake(void **p) { *p = &opened; return 0; }
static int any_fake(snd_pcm_t *p, void *hw) { (void)p; (void)hw; return 0; }
static int unsigned_fake(snd_pcm_t *p, void *hw, unsigned v) {
    (void)p; (void)hw; (void)v; return 0;
}
static int rate_fake(snd_pcm_t *p, void *hw, unsigned v, int d) {
    (void)d; return unsigned_fake(p, hw, v);
}
static int period_fake(snd_pcm_t *p, void *hw, unsigned long v, int d) {
    (void)d; return unsigned_fake(p, hw, (unsigned)v);
}
static void free_fake(void *p) { (void)p; }
static int hwp_fake(snd_pcm_t *p, void *hw) {
    (void)p; (void)hw; return fail_hwp ? -22 : 0;
}
static int prepare_fake(snd_pcm_t *p) {
    (void)p;
    if (fail_prepare_n > 0) { fail_prepare_n--; return -22; }
    ++prepared;
    return 0;
}
static int write_fake(snd_pcm_t *p, const void *d, unsigned long c) {
    (void)p; (void)d; return (int)c;
}
static int close_fake(snd_pcm_t *p) { (void)p; ++closed; return 0; }
static int no_op(snd_pcm_t *p) { (void)p; return 0; }
static const char *error_fake(int e) { (void)e; return "test error"; }

static const xr_audio_output_ops_t ops = {
    .a_open = open_fake, .a_hwp_malloc = alloc_fake, .a_hwp_any = any_fake,
    .a_set_access = unsigned_fake, .a_set_format = unsigned_fake,
    .a_set_channels = unsigned_fake, .a_set_rate = rate_fake,
    .a_set_ps = period_fake, .a_set_periods = rate_fake,
    .a_hwp = hwp_fake, .a_hwp_free = free_fake, .a_prepare = prepare_fake,
    .a_writei = write_fake, .a_drop = no_op, .a_close = close_fake,
    .a_drain = no_op, .a_strerror = error_fake,
};

static void reset_counters(void) {
    opened = closed = prepared = open_attempts = 0;
}

int main(void) {
    int16_t pcm[1440] = {0};

    /* A. 冷态 + prepare 失败: init 的 3 次重试会各开一个句柄,
          修复前三个全部泄漏(spk 保持非 NULL, 后续永远走复用分支)。 */
    reset_counters();
    fail_prepare_n = 99;                  /* 让 init 的 3 次重试全部失败 */
    (void)xr_player_init(&ops);
    CHECK(open_attempts == 3, "init 的 SAI 重试应尝试 3 次");
    CHECK(opened == closed,
          "冷态 prepare 失败: 每次 open 都必须 close, 修复前 init 重试放大泄漏 3 倍");
    fail_prepare_n = 0;                  /* 显式清标志: 不许污染后面的用例 */
    CHECK(xr_player_start() == 0,
          "prepare 失败后必须还能冷态重开(修复前 spk 是僵尸句柄, 永久失败)");
    xr_player_push_pcm(pcm, sizeof pcm / sizeof pcm[0]);
    xr_player_finish();

    /* B. hw_params 失败: 这条路径本来就有 close, 守住不退化 */
    reset_counters();
    fail_hwp = 1;
    (void)xr_player_init(&ops);
    CHECK(opened == closed, "hw_params 失败也必须 close 每个句柄");
    fail_hwp = 0;
    (void)xr_player_init(&ops);

    /* C. open 失败: 没有句柄可关, 不能凭空多关 */
    reset_counters();
    fail_open = 1;
    (void)xr_player_init(&ops);           /* spk 常驻, 重试循环不跑, 本次可能没触发 */
    CHECK(closed == 0, "open 失败时不应调用 close");
    fail_open = 0;                        /* 显式清标志: 不许污染后面的用例 */

    /* D. 复用分支: spk 常驻后 prepare 再失败, 必须让 spk_open 返回失败。
          修复前 `if (spk) { a_prepare(spk); return 0; }` 丢弃返回值,
          调用方以为声卡就绪, 播放线程对未 prepare 的句柄写必然出错。 */
    reset_counters();
    CHECK(xr_player_start() == 0, "先建立常驻句柄");
    xr_player_finish();
    fail_prepare_n = 1;
    CHECK(xr_player_start() != 0,
          "复用分支 prepare 失败必须返回失败, 不能吞掉错误码");
    CHECK(xr_player_start() == 0, "复用分支失败后必须能再次恢复");
    xr_player_push_pcm(pcm, sizeof pcm / sizeof pcm[0]);
    xr_player_finish();

    fprintf(stderr, failures ? "audio_player_open_test: %d FAILED\n"
                             : "audio_player_open_test: OK\n", failures);
    return failures ? 1 : 0;
}