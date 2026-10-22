/* PC(x86_64) ASan 探针: 复刻板上 2mic_2ref VQE 调用几何, 验证原厂库越界.
 * 库本身带 libasan(x86 构建已插桩) → 越界写入/读出当场报告, 栈直达厂商函数.
 * 几何(与 vendor/rkaudio-vqe/pcm_rkaudio_vqe.c 同源):
 *   init_by_conf(48000, 16, src=2, ref=2, json);  每帧 in=768*4ch shorts, out=768*1ch shorts */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

extern void *rkaudio_preprocess_init_by_conf(int rate, int bits, int src_chan,
                                             int ref_chan, char *conf_path);
extern int rkaudio_preprocess_short(void *st, short *in, short *out,
                                    int in_size, int *wakeup_status);
extern void rkaudio_preprocess_destory(void *st);
extern int rkaudio_out_chan_invoke(void *st);

int main(int argc, char **argv) {
    const char *conf = argc > 1 ? argv[1] : "config.json";
    long nframes = argc > 2 ? atol(argv[2]) : 6000;
    enum { FRAME = 768, IN_CH = 4, OUT_CH = 1 };    /* 2mic+2ref 交织, JSON out_channel=1 */
    int in_n = FRAME * IN_CH, out_n = FRAME * OUT_CH;

    void *st = rkaudio_preprocess_init_by_conf(48000, 16, 2, 2, (char *)conf);
    if (!st) { fprintf(stderr, "init_by_conf 失败\n"); return 2; }
    if (rkaudio_out_chan_invoke(st) != OUT_CH) {
        fprintf(stderr, "output channels differ from fixed probe geometry; use capacity_probe.c\n");
        return 2;
    }

    short *in = malloc((size_t)in_n * sizeof(short));
    short *out = malloc((size_t)out_n * sizeof(short));   /* 精确尺寸: 红区紧贴 */
    if (!in || !out) return 2;

    /* 远端(参考/回声源): 语音样包络调制的扫频; 近端: 慢变语音样信号 + 6ms 延迟衰减回声 */
    long t = 0; int wake = 0;
    for (long f = 0; f < nframes; f++) {
        for (int i = 0; i < FRAME; i++, t++) {
            double env = 0.5 * (1 + sin(2 * M_PI * 3.0 * t / 48000.0));   /* 3Hz 音节包络 */
            double far = 6000 * env * sin(2 * M_PI * (400 + 1200 * (t % 48000) / 48000.0) * t / 48000.0);
            double near = 5000 * sin(2 * M_PI * 220.0 * t / 48000.0)
                        * (0.5 + 0.5 * sin(2 * M_PI * 2.0 * t / 48000.0));
            double echo = 0.5 * far;                    /* 板上声学回声的粗模拟(无延迟环内自有) */
            in[i * IN_CH + 0] = (short)(near + echo);
            in[i * IN_CH + 1] = (short)(0.9 * near + 0.8 * echo);
            in[i * IN_CH + 2] = (short)far;
            in[i * IN_CH + 3] = (short)(0.95 * far);
        }
        int returned = rkaudio_preprocess_short(st, in, out, in_n, &wake);
        if (returned != out_n * (int)sizeof(short)) {
            fprintf(stderr, "unexpected output bytes: %d\n", returned); return 1;
        }
        if (f % 1000 == 0) { fprintf(stderr, "frame %ld out[0]=%d\n", f, out[0]); fflush(stderr); }
    }
    printf("clean: %ld 帧 (%.0fs 音频) 无 ASan 报告\n", nframes, nframes * 768.0 / 48000.0);
    free(in); free(out);
    rkaudio_preprocess_destory(st);
    printf("destory 也干净\n");
    return 0;
}
