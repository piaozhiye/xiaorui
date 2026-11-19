/* cap_accum 回归测试 —— 钉死 2026-11-19 板端 SIGSEGV 的根因。
 *
 * 事故: 旧实现 n=1536 时再写 256 样本 -> 写穿 g_cap_acc[1600] 共 384 字节,
 * 砸中紧邻全局(g_cap_acc_n/g_pending_n/g_original_argv/看门狗心跳),
 * 板端每轮回答期崩(rc=139 x2)。
 *
 * 契约: pull 之后 n 永远 <= FRAME_SAMPLES; 物理上一步都不许越过数组。 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "cap_accum.h"

#define CANARY_SAMPLES 256

/* 把累加器包进带金丝雀的结构: 写穿 buf 的第一步就会踩到 canary */
struct guarded {
    cap_accum_t acc;
    int16_t canary[CANARY_SAMPLES];
};

static struct guarded g;
static int g_supply;          /* 每次读供给的样本数(模拟 VQE 周期) */
static int16_t g_next_value;  /* 供给数据的下一个取值(校验内容顺序) */

static long fake_reader(void *dev, int16_t *dst, unsigned long want)
{
    (void)dev;
    long give = (long)want < g_supply ? (long)want : g_supply;
    for (long i = 0; i < give; i++) dst[i] = g_next_value++;
    return give;
}

static long epipe_reader(void *dev, int16_t *dst, unsigned long want)
{
    (void)dev; (void)dst; (void)want;
    return -EPIPE;
}

static int failures;

#define CHECK(cond, msg) do { \
    if (!(cond)) { \
        fprintf(stderr, "FAIL: %s (line %d)\n", msg, __LINE__); \
        failures++; \
    } \
} while (0)

static void canary_reset(void)
{
    for (int i = 0; i < CANARY_SAMPLES; i++) g.canary[i] = (int16_t)(0x5AA5 + i);
}

static int canary_intact(void)
{
    for (int i = 0; i < CANARY_SAMPLES; i++)
        if (g.canary[i] != (int16_t)(0x5AA5 + i)) return 0;
    return 1;
}

/* 用例1(事故复现): 每步 256 样本攒帧 —— n 到 1536 后再拉一步,
 * 旧实现写穿到 1792 并踩 canary; 修复后钳到 1600, 一步不越界 */
static void test_bounds_with_256_step(void)
{
    memset(&g, 0, sizeof g);
    canary_reset();
    cap_accum_init(&g.acc);
    g_supply = 256;
    g_next_value = 1000;

    int pulls = 0;
    while (!cap_accum_ready(&g.acc)) {
        int r = cap_accum_pull(&g.acc, fake_reader, NULL, 256);
        CHECK(r > 0, "每步都应读到数据");
        CHECK(g.acc.n <= FRAME_SAMPLES, "pull 后 n 不得越过帧界(写穿即事故)");
        CHECK(canary_intact(), "canary 必须完好(物理越界检测)");
        if (++pulls > 100) { CHECK(0, "死循环: 永远攒不满一帧"); return; }
    }
    CHECK(pulls >= 6, "1600/256 至少 7 步攒满");

    int16_t frame[FRAME_SAMPLES];
    cap_accum_take(&g.acc, frame);
    CHECK(frame[0] == 1000 && frame[FRAME_SAMPLES - 1] == 1000 + FRAME_SAMPLES - 1,
          "整帧内容顺序应连续");
    CHECK(g.acc.n >= 0 && g.acc.n <= FRAME_SAMPLES, "取帧后剩余量仍在界内");
}

/* 用例2: 设备给的比要的少(供给 100/步) —— 内容顺序不能乱 */
static void test_partial_supply(void)
{
    memset(&g, 0, sizeof g);
    canary_reset();
    cap_accum_init(&g.acc);
    g_supply = 100;
    g_next_value = -500;

    while (!cap_accum_ready(&g.acc))
        cap_accum_pull(&g.acc, fake_reader, NULL, 256);
    int16_t frame[FRAME_SAMPLES];
    cap_accum_take(&g.acc, frame);
    CHECK(frame[0] == -500 && frame[FRAME_SAMPLES - 1] == -500 + FRAME_SAMPLES - 1,
          "分步攒满的帧内容应连续");
}

/* 用例3: 设备错误(EPIPE) —— 错误透传, 累加器清空重来 */
static void test_error_resets(void)
{
    memset(&g, 0, sizeof g);
    cap_accum_init(&g.acc);
    g_supply = 256;
    cap_accum_pull(&g.acc, fake_reader, NULL, 256); /* 先攒 256 */
    int r = cap_accum_pull(&g.acc, epipe_reader, NULL, 256);
    CHECK(r == -EPIPE, "设备错误应透传");
    CHECK(g.acc.n == 0, "出错后累加器应清空(丢弃半帧)");
    CHECK(!cap_accum_ready(&g.acc), "清空后不应就绪");
}

/* 用例4: 攒满后取帧, 剩余前移 —— 下一帧从正确位置开始 */
static void test_leftover_carries_over(void)
{
    memset(&g, 0, sizeof g);
    canary_reset();
    cap_accum_init(&g.acc);
    g_supply = 200; /* 8 步攒 1600, 无剩余 */
    g_next_value = 1;
    while (!cap_accum_ready(&g.acc))
        cap_accum_pull(&g.acc, fake_reader, NULL, 200);
    int16_t f1[FRAME_SAMPLES], f2[FRAME_SAMPLES];
    cap_accum_take(&g.acc, f1);
    g_supply = 200; /* 继续供给 */
    while (!cap_accum_ready(&g.acc))
        cap_accum_pull(&g.acc, fake_reader, NULL, 200);
    cap_accum_take(&g.acc, f2);
    CHECK(f2[0] == 1 + FRAME_SAMPLES, "第二帧应紧接第一帧(剩余前移正确)");
}

int main(void)
{
    test_bounds_with_256_step();
    test_partial_supply();
    test_error_resets();
    test_leftover_carries_over();
    if (failures) {
        fprintf(stderr, "cap_accum: %d 项失败\n", failures);
        return 1;
    }
    printf("cap_accum: 全部通过\n");
    return 0;
}
