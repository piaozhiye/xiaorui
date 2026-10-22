/* xr_mic4 主机确定性单测: 通道抽取边界/抗混叠抽取连续性/输出数/混叠抑制/reset
 * (GLM-ES8388-MIC-REF-PLAN.md 阶段2 门禁要求) */
#include "xr_mic4.h"
#include <math.h>
#include <stdio.h>
#include <string.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static int fails;
#define CHECK(c, msg) do { if (!(c)) { printf("FAIL: %s\n", msg); fails++; } } while (0)

static void tone(int16_t *buf, int n, double hz, double sr, double amp) {
    for (int i = 0; i < n; i++)
        buf[i] = (int16_t)(amp * sin(2.0 * M_PI * hz * i / sr));
}

static double rms(const int16_t *b, int n) {
    double s = 0;
    for (int i = 0; i < n; i++) s += (double)b[i] * b[i];
    return sqrt(s / (n ? n : 1));
}

int main(void) {
    /* 1. 通道抽取: 正确性与越界显式失败 */
    int16_t inter[8] = {10, 11, 12, 13, 20, 21, 22, 23};   /* 2ch×4帧 */
    int16_t mono[4];
    CHECK(xr_mic4_pick(inter, 4, 2, 0, mono) == 4 && mono[0] == 10 && mono[3] == 22,
          "pick ch0");
    CHECK(xr_mic4_pick(inter, 4, 2, 1, mono) == 4 && mono[0] == 11 && mono[3] == 23,
          "pick ch1");
    CHECK(xr_mic4_pick(inter, 4, 2, 2, mono) == -1, "mic_ch 越界拒绝");
    CHECK(xr_mic4_pick(inter, 4, 0, 0, mono) == -1, "hw_channels=0 拒绝");
    CHECK(xr_mic4_pick(inter, 4, 2, -1, mono) == -1, "负 mic_ch 拒绝");

    /* 2. 抽取器输出数与非 3 倍数输入拒绝 */
    xr_deci3_t d;
    xr_deci3_init(&d);
    int16_t in[960], out[320];
    tone(in, 960, 1000.0, 48000.0, 8000.0);
    CHECK(xr_deci3_run(&d, in, 960, out) == 320, "输出数 = in/3");
    CHECK(xr_deci3_run(&d, in, 950, out) == -1, "非 3 倍数输入拒绝");

    /* 3. 跨块连续性: 分块处理 == 一次性处理 */
    xr_deci3_t a, b;
    xr_deci3_init(&a); xr_deci3_init(&b);
    enum { N = 960 * 4 };
    static int16_t big[N], o1[N / 3], o2[N / 3], tmp[N / 3];
    tone(big, N, 1000.0, 48000.0, 8000.0);
    xr_deci3_run(&a, big, N, o1);
    int got = 0;
    for (int off = 0; off < N; off += 960) {         /* 4 块, 含 960<keep 的短块路径 */
        int n = xr_deci3_run(&b, big + off, 960, tmp);
        CHECK(n == 320, "短块输出数");
        memcpy(o2 + got, tmp, (size_t)n * sizeof(int16_t));
        got += n;
    }
    CHECK(got == N / 3 && memcmp(o1, o2, sizeof(o1)) == 0, "分块==一次性(状态连续)");

    /* 3b. 小块分支: 30+60 是实测越界反例(hist_n=30 再拼 60 超 71 容量);
       逐小块喂必须与整块喂逐样本一致, 且总输出数不变 */
    {
        static int16_t small[N], os[N / 3];
        for (int i = 0; i < N; i++)
            small[i] = (int16_t)((i * 2654435761u >> 13) % 16001 - 8000);  /* 确定性伪随机 */
        xr_deci3_reset(&a);
        xr_deci3_run(&a, small, N, os);
        const int chunks[] = {30, 60, 3, 63, 66, 72, 960};   /* 全 3 倍数, 覆盖 </>keep */
        for (size_t ci = 0; ci < sizeof(chunks) / sizeof(chunks[0]); ci++) {
            int step = chunks[ci];
            xr_deci3_reset(&b);
            static int16_t oc[N / 3];
            int og = 0, off = 0;
            while (off < N) {                     /* 尾块收齐: 余量也喂完 */
                if (off + step > N) step = N - off;
                step -= step % 3;
                if (step == 0) break;
                og += xr_deci3_run(&b, small + off, step, oc + og);
                off += step;
            }
            CHECK(og == N / 3, "小块总输出数不变");
            CHECK(memcmp(os, oc, (size_t)og * sizeof(int16_t)) == 0,
                  "小块(step=任意)==整块逐样本一致");
        }
        /* 交错小块: 30,60,30,60,... 混合喂(尾块收齐) */
        static int16_t oc2[N / 3];
        xr_deci3_reset(&b);
        int og = 0, off = 0, alt = 0;
        while (off < N) {
            int step = alt ? 60 : 30; alt = !alt;
            if (off + step > N) step = N - off;
            step -= step % 3;
            if (step == 0) break;
            og += xr_deci3_run(&b, small + off, step, oc2 + og);
            off += step;
        }
        CHECK(og == N / 3 && memcmp(os, oc2, (size_t)og * sizeof(int16_t)) == 0,
              "交错小块(30/60混合)==整块逐样本一致");
    }

    /* 4. 混叠抑制: 10kHz@48k → 16k 输出应近零; 1kHz 通带保留 */
    xr_deci3_init(&a);
    tone(big, N, 10000.0, 48000.0, 8000.0);
    xr_deci3_run(&a, big, N, o1);
    double alias_rms = rms(o1 + 200, N / 3 - 200);
    xr_deci3_init(&a);
    tone(big, N, 1000.0, 48000.0, 8000.0);
    xr_deci3_run(&a, big, N, o1);
    double pass_rms = rms(o1 + 200, N / 3 - 200);
    printf("info: pass_rms=%.1f alias_rms=%.1f 比值=%.4f\n",
           pass_rms, alias_rms, alias_rms / pass_rms);
    CHECK(alias_rms / pass_rms < 0.02, "混叠抑制比 < 2%");

    /* 5. reset 后两次独立运行一致 */
    xr_deci3_reset(&a);
    xr_deci3_run(&a, big, N, o2);
    CHECK(memcmp(o1, o2, sizeof(o1)) == 0, "reset 状态清零");

    /* 6. 端到端: 4ch 交织, 1kHz 只在 ch2, mic=ch0 应近静默 / mic=ch2 应有音 */
    enum { FR = 960 };
    static int16_t i4[FR * 4];
    memset(i4, 0, sizeof(i4));
    for (int i = 0; i < FR; i++)
        i4[i * 4 + 2] = (int16_t)(8000 * sin(2 * M_PI * 1000.0 * i / 48000.0));
    int16_t m0[FR], m2[FR];
    xr_mic4_pick(i4, FR, 4, 0, m0);
    xr_mic4_pick(i4, FR, 4, 2, m2);
    CHECK(rms(m0, FR) < 1, "未选通道不串音");
    CHECK(rms(m2, FR) > 5000, "选中通道保幅");
    xr_deci3_init(&a);
    CHECK(xr_deci3_run(&a, m2, FR, tmp) == FR / 3, "4ch 链路输出数");

    printf(fails ? "RESULT fail=%d\n" : "RESULT pass\n", fails);
    return fails ? 1 : 0;
}
