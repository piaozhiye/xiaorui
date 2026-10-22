#ifndef XR_MIC4_H
#define XR_MIC4_H
/*
 * ES8388 阶段2 采集适配: 48k/Nch(S16交织) → 指定单 mic 通道 → 抗混叠 ÷3 → 16k/mono.
 * (docs/GLM-ES8388-MIC-REF-PLAN.md 阶段2; 默认 USB asr_mic 16k/1ch 路径不受影响)
 *
 * 纯 DSP 部分(无 ALSA, 主机可测):
 *   xr_deci3_t      有状态多相 FIR 抗混叠抽取器(48k→16k, 不允许隔3取1)
 *   xr_mic4_pick    交织帧按 mic 通道抽取(越界显式失败)
 * 未接 mic 通道不求平均、不送下游; ref 通道(ch2/ch3)不混入 mic。
 */
#include <stdint.h>

#define XR_DECI3_TAPS_PER_PHASE 24   /* 72 阶原型, 海明窗低通 */

typedef struct {
    float proto[XR_DECI3_TAPS_PER_PHASE * 3];      /* 72 阶原型低通 */
    int16_t hist[XR_DECI3_TAPS_PER_PHASE * 3 - 1]; /* 跨块滑动窗口 */
    int hist_n;
    int phase;                                      /* 绝对样本计数 mod 3 */
} xr_deci3_t;

void xr_deci3_init(xr_deci3_t *d);
void xr_deci3_reset(xr_deci3_t *d);
/* 输入 48k mono, 输出 16k mono; 返回输出样本数, out 容量须 ≥ in_n/3 */
int xr_deci3_run(xr_deci3_t *d, const int16_t *in, int in_n, int16_t *out);

/* 从 hw_channels 通道交织帧中取第 mic_ch 通道到 mono; 越界/空参返回 -1 */
int xr_mic4_pick(const int16_t *interleaved, int frames, int hw_channels,
                 int mic_ch, int16_t *mono_out);

#endif
