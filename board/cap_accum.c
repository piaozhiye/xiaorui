#include "cap_accum.h"

#include <string.h>

void cap_accum_init(cap_accum_t *a)
{
    a->n = 0;
}

int cap_accum_pull(cap_accum_t *a, cap_reader_t rd, void *dev, int step)
{
    /* ★ 事故修复(2026-11-19): 请求量必须钳到"距满帧的余量"。
     * 旧实现无脑写 step 样本 —— n=1536 时写 256 越界 384 字节, 砸中
     * BSS 紧邻全局, 板端每轮回答期段错误(rc=139 x2)。 */
    int room = FRAME_SAMPLES - a->n;
    if (room <= 0) return 0; /* 已满(调用方应先 take); 不再读 */
    int want = step < room ? step : room;
    long r = rd(dev, a->buf + a->n, (unsigned long)want);
    if (r < 0) {
        a->n = 0;
        return (int)r;
    }
    a->n += (int)r;
    return (int)r;
}

int cap_accum_ready(const cap_accum_t *a)
{
    return a->n >= FRAME_SAMPLES;
}

void cap_accum_take(cap_accum_t *a, int16_t out[FRAME_SAMPLES])
{
    memcpy(out, a->buf, (size_t)FRAME_SAMPLES * 2);
    memmove(a->buf, a->buf + FRAME_SAMPLES,
            (size_t)(a->n - FRAME_SAMPLES) * 2);
    a->n -= FRAME_SAMPLES;
}
