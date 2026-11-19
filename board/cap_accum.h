#ifndef XR_CAP_ACCUM_H
#define XR_CAP_ACCUM_H

#include <stdint.h>

#include "xr_audio_format.h" /* FRAME_SAMPLES */

/* 采集累加器: 设备侧小步读(一个 VQE 周期), 攒够一整帧(100ms)再喂下游。
 *
 * 2026-11-19 板端事故(rc=139 SIGSEGV x2, 崩在每轮回答期): 旧实现无上限地
 * 往 buf+n 写 step 样本 —— n=1536 时再写 256 会写穿 384 字节, 恰好砸中
 * BSS 里紧邻的 g_cap_acc_n / g_pending_n / g_original_argv / 看门狗心跳
 * g_loop_tick_ms(nm 实测 0x4960c8 起), 每个都能段错误或让看门狗误动作。
 * RESPOND 等待期每 100ms 走一次, 几秒内必炸。
 *
 * 契约(测试钉死): pull 之后 n 永远 <= FRAME_SAMPLES, 不越帧界一步。 */

typedef long (*cap_reader_t)(void *dev, int16_t *dst, unsigned long want);

typedef struct {
    int16_t buf[FRAME_SAMPLES];
    int n;
} cap_accum_t;

void cap_accum_init(cap_accum_t *a);

/* 读一小步进累加器; 返回本步读到的样本数, <0 透传设备错误(并清空累加器)。
 * step 由调用方给(设备周期); 实际读取量会被钳到"距满帧的余量"。 */
int cap_accum_pull(cap_accum_t *a, cap_reader_t rd, void *dev, int step);

/* 累加器里够一整帧了吗 */
int cap_accum_ready(const cap_accum_t *a);

/* 取走一整帧(1600 样本)到 out, 剩余样本前移; 调用前必须 ready() */
void cap_accum_take(cap_accum_t *a, int16_t out[FRAME_SAMPLES]);

#endif
