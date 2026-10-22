#include "xr_mic4.h"
#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

enum { PROTO = XR_DECI3_TAPS_PER_PHASE * 3 };   /* 72 阶原型 */

/* 海明窗低通, 截止 6.8kHz@48k: 通带内平坦, 8kHz 起进入阻带(16k 奈奎斯特之外) */
void xr_deci3_init(xr_deci3_t *d) {
    double fc = 6800.0 / 48000.0, sum = 0.0;
    for (int i = 0; i < PROTO; i++) {
        double t = (double)(i - (PROTO - 1) / 2);
        double sinc = (fabs(t) < 1e-9) ? 2.0 * M_PI * fc : sin(2.0 * M_PI * fc * t) / t;
        double w = 0.54 - 0.46 * cos(2.0 * M_PI * i / (PROTO - 1));
        d->proto[i] = sinc * w;
        sum += d->proto[i];
    }
    for (int i = 0; i < PROTO; i++) d->proto[i] /= sum;
    xr_deci3_reset(d);
}

void xr_deci3_reset(xr_deci3_t *d) {
    memset(d->hist, 0, sizeof(d->hist));
    d->hist_n = 0;
    d->phase = 0;          /* 绝对样本计数 mod 3, 跨块保持对齐 */
}

/* 虚拟窗口取值: 位置 p 相对"hist+in"拼接流(p<0 不出现——调用点保证≥-hist_n) */
static inline double win_at(const xr_deci3_t *d, const int16_t *in, int p) {
    return (p < d->hist_n) ? d->hist[p] : in[p - d->hist_n];
}

int xr_deci3_run(xr_deci3_t *d, const int16_t *in, int in_n, int16_t *out) {
    if (in_n < 0 || (in_n % 3) != 0) return -1;   /* 只接受 3 的整倍数(帧对齐) */
    int out_n = 0;
    for (int i = 0; i < in_n; i++) {
        if (d->phase == 0) {                       /* 采样点: 卷积 72 阶 */
            int end = d->hist_n + i;               /* 窗口末样本位置 */
            double acc = 0.0;
            for (int k = 0; k < PROTO; k++) {
                int p = end - k;
                if (p >= 0) acc += win_at(d, in, p) * d->proto[k];
            }
            double v = acc > 32767 ? 32767 : acc < -32768 ? -32768 : acc;
            out[out_n++] = (int16_t)lround(v);
        }
        d->phase = (d->phase + 1) % 3;
    }
    /* 滚动历史: 拼接流(hist[0..hist_n) + in[0..in_n))保留末 keep 个样本.
       单一规则覆盖全部块长——老三分支两条坏路(实测 ASan 越界+分块不一致):
       hist_n<in_n 直写 hist+hist_n 可越过 keep(30+60>71);
       hist_n>=in_n 分支丢历史且把 hist_n 错置 keep. */
    int keep = PROTO - 1;
    int total = d->hist_n + in_n;
    if (total > keep) {
        int drop = total - keep;                    /* 拼接流前 drop 个丢弃 */
        int hist_keep = d->hist_n > drop ? d->hist_n - drop : 0;
        int in_off = hist_keep > 0 ? 0 : drop - d->hist_n;  /* 大块: 取末段 */
        if (hist_keep > 0)
            memmove(d->hist, d->hist + drop, (size_t)hist_keep * sizeof(int16_t));
        memcpy(d->hist + hist_keep, in + in_off,
               (size_t)(keep - hist_keep) * sizeof(int16_t));
        d->hist_n = keep;
    } else {
        memcpy(d->hist + d->hist_n, in, (size_t)in_n * sizeof(int16_t));
        d->hist_n = total;
    }
    return out_n;
}

int xr_mic4_pick(const int16_t *interleaved, int frames, int hw_channels,
                 int mic_ch, int16_t *mono_out) {
    if (!interleaved || !mono_out || frames < 0) return -1;
    if (hw_channels < 1 || mic_ch < 0 || mic_ch >= hw_channels) return -1;
    for (int i = 0; i < frames; i++)
        mono_out[i] = interleaved[i * hw_channels + mic_ch];
    return frames;
}
