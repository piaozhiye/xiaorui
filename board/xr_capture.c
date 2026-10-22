/* xz_capture -- 单进程板端语音采集前端.
 *
 * 自己 snd_pcm_readi 直读 USB 麦 (16k mono 16bit), 无 arecord 子进程:
 *   - 帧级能量 VAD: 100ms 帧, 实时判
 *   - 等待说话: 静音期环境底噪自适应门 + 3 帧人声即确认
 *   - 说完即停: 人声后连续 trailing 静音 (0.4s) 就停, 不死等窗口
 *   - 输出: 带头 16k wav 到 --out, 并打印各阶段耗时到 stderr
 *
 * 用法: xz_capture --out /tmp/cap.wav [--wait 10] [--max 12]
 * 退出码: 0=录到话  4=窗口内没人说话  2=设备错误
 */
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <fcntl.h>
#include <errno.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>


typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
typedef long snd_pcm_sframes_t;

/* ---- ALSA via dlopen: 工具链 alsa 头 1.1.5 与板上 libasound 1.2.11 的插件 ABI
   不兼容(直链会拿到坏流), 这里运行时加载板子自己的库, 全部走 dlsym ---- */
static void *alsa_lib;
static int (*p_snd_pcm_open)(snd_pcm_t **, const char *, int, int);
static int (*p_snd_pcm_hw_params_any)(snd_pcm_t *, snd_pcm_hw_params_t *);
static int (*p_snd_pcm_hw_params_set_access)(snd_pcm_t *, snd_pcm_hw_params_t *, int);
static int (*p_snd_pcm_hw_params_set_format)(snd_pcm_t *, snd_pcm_hw_params_t *, int);
static int (*p_snd_pcm_hw_params_set_channels)(snd_pcm_t *, snd_pcm_hw_params_t *, unsigned);
static int (*p_snd_pcm_hw_params_set_rate)(snd_pcm_t *, snd_pcm_hw_params_t *, unsigned int, int);
static int (*p_snd_pcm_hw_params_set_period_size)(snd_pcm_t *, snd_pcm_hw_params_t *, unsigned long, int);
static int (*p_snd_pcm_hw_params_set_periods)(snd_pcm_t *, snd_pcm_hw_params_t *, unsigned int, int);
static int (*p_snd_pcm_hw_params)(snd_pcm_t *, snd_pcm_hw_params_t *);
static int (*p_snd_pcm_prepare)(snd_pcm_t *);
static int (*p_snd_pcm_start)(snd_pcm_t *);
static long (*p_snd_pcm_readi)(snd_pcm_t *, void *, unsigned long);
static int (*p_snd_pcm_drop)(snd_pcm_t *);
static int (*p_snd_pcm_close)(snd_pcm_t *);
static const char *(*p_snd_strerror)(int);
static int (*p_snd_pcm_hw_params_malloc)(snd_pcm_hw_params_t **);
typedef struct _snd_pcm_sw_params snd_pcm_sw_params_t;
static int (*p_snd_pcm_sw_params_malloc)(snd_pcm_sw_params_t **);
static int (*p_snd_pcm_sw_params_current)(snd_pcm_t *, snd_pcm_sw_params_t *);
static int (*p_snd_pcm_sw_params_set_avail_min)(snd_pcm_t *, snd_pcm_sw_params_t *, unsigned long);
static int (*p_snd_pcm_sw_params_set_start_threshold)(snd_pcm_t *, snd_pcm_sw_params_t *, unsigned long);
static int (*p_snd_pcm_sw_params)(snd_pcm_t *, snd_pcm_sw_params_t *);
static int (*p_snd_pcm_sw_params_free)(snd_pcm_sw_params_t *);
static int (*p_snd_pcm_hw_params_get_period_size)(const snd_pcm_hw_params_t *, unsigned long *, int *);
static void (*p_snd_pcm_hw_params_free)(snd_pcm_hw_params_t *);
#define PCM_CAPTURE 1  /* SND_PCM_STREAM_CAPTURE, 值在 1.x/2.x 稳定 */
#define FMT_S16 2      /* SND_PCM_FORMAT_S16_LE */
#define ACC_RW_INTERLEAVED 3

static int alsa_load(void) {
    alsa_lib = dlopen("libasound.so.2", RTLD_NOW);
    if (!alsa_lib) { fprintf(stderr, "[xz_capture] dlopen libasound: %s\n", dlerror()); return -1; }
    struct { const char *n; void **p; } tab[] = {
        {"snd_pcm_open", (void**)&p_snd_pcm_open},
        {"snd_pcm_hw_params_any", (void**)&p_snd_pcm_hw_params_any},
        {"snd_pcm_hw_params_set_access", (void**)&p_snd_pcm_hw_params_set_access},
        {"snd_pcm_hw_params_set_format", (void**)&p_snd_pcm_hw_params_set_format},
        {"snd_pcm_hw_params_set_channels", (void**)&p_snd_pcm_hw_params_set_channels},
        {"snd_pcm_hw_params_set_rate", (void**)&p_snd_pcm_hw_params_set_rate},
        {"snd_pcm_hw_params_set_period_size", (void**)&p_snd_pcm_hw_params_set_period_size},
        {"snd_pcm_hw_params_set_periods", (void**)&p_snd_pcm_hw_params_set_periods},
        {"snd_pcm_hw_params", (void**)&p_snd_pcm_hw_params},
        {"snd_pcm_prepare", (void**)&p_snd_pcm_prepare},
        {"snd_pcm_start", (void**)&p_snd_pcm_start},
        {"snd_pcm_readi", (void**)&p_snd_pcm_readi},
        {"snd_pcm_drop", (void**)&p_snd_pcm_drop},
        {"snd_pcm_close", (void**)&p_snd_pcm_close},
        {"snd_strerror", (void**)&p_snd_strerror},
        {"snd_pcm_hw_params_malloc", (void**)&p_snd_pcm_hw_params_malloc},
        {"snd_pcm_hw_params_free", (void**)&p_snd_pcm_hw_params_free},
        {"snd_pcm_sw_params_malloc", (void**)&p_snd_pcm_sw_params_malloc},
        {"snd_pcm_sw_params_current", (void**)&p_snd_pcm_sw_params_current},
        {"snd_pcm_sw_params_set_avail_min", (void**)&p_snd_pcm_sw_params_set_avail_min},
        {"snd_pcm_sw_params_set_start_threshold", (void**)&p_snd_pcm_sw_params_set_start_threshold},
        {"snd_pcm_sw_params", (void**)&p_snd_pcm_sw_params},
        {"snd_pcm_sw_params_free", (void**)&p_snd_pcm_sw_params_free},
        {"snd_pcm_hw_params_get_period_size", (void**)&p_snd_pcm_hw_params_get_period_size},
    };
    for (size_t i = 0; i < sizeof(tab)/sizeof(tab[0]); i++) {
        *tab[i].p = dlsym(alsa_lib, tab[i].n);
        if (!*tab[i].p) { fprintf(stderr, "[xz_capture] dlsym %s: %s\n", tab[i].n, dlerror()); return -1; }
    }
    return 0;
}


#define RATE 16000
#define FRAME_MS 100
#define FRAME_SAMPLES (RATE * FRAME_MS / 1000)

static volatile sig_atomic_t got_sig = 0;
static void on_sig(int s) { (void)s; got_sig = 1; }

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
}

static double frame_db(const int16_t *p, int n) {
    int64_t sum = 0;
    for (int i = 0; i < n; i++) sum += (int64_t)p[i] * p[i];
    double power = (double)sum / n / (32768.0 * 32768.0);
    return power > 0 ? 10.0 * log10(power) : -120.0;
}

/* 48k 原生帧 3:1 均值降采样到 16k, 原地写回 frame 前 n/3 个样本 (asr_mic 直开 16k 时不用) */
static void decimate(int16_t *p, int hw_n) {
    (void)p; (void)hw_n;
}

static void write_wav(FILE *f, const int16_t *pcm, size_t frames) {
    /* 整头打包一次写: 逐字段 fwrite 在这块板的 libc 上出现过 3 字节错位 */
    unsigned char h[44] = {
        'R','I','F','F', 0,0,0,0, 'W','A','V','E','f','m','t',' ',
        16,0,0,0, 1,0, 1,0, 0,0,0,0, 0,0,0,0, 2,0, 16,0,
        'd','a','t','a', 0,0,0,0
    };
    uint32_t data_len = (uint32_t)(frames * 2);
    uint32_t riff = 36 + data_len, sr = RATE, br = RATE * 2;
    memcpy(h + 4, &riff, 4);
    memcpy(h + 24, &sr, 4);
    memcpy(h + 28, &br, 4);
    memcpy(h + 40, &data_len, 4);
    fwrite(h, 1, 44, f);
    fwrite(pcm, 2, frames, f);
}

int main(int argc, char **argv) {
    const char *out_path = "/tmp/xz_turn_capture.wav";
    double wait_max = 10.0, rec_max = 12.0;
    const char *dev = getenv("XR_MIC");
    if (!dev) dev = "asr_mic";   /* /etc/asound.conf: type plug -> hw:Audio,0, 字幕项目验证稳 */
    /* 门限: 环境底噪 dB + margin; 也可用 XZ_VAD_MARGIN 覆盖 */
    double margin = 7.0;
    double trailing_s = 0.4;   /* 人声后静音多久算说完 */
    int min_voice_frames = 3;  /* 至少 3 帧(0.3s)人声才算说话 */

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--out") && i + 1 < argc) out_path = argv[++i];
        else if (!strcmp(argv[i], "--wait") && i + 1 < argc) wait_max = atof(argv[++i]);
        else if (!strcmp(argv[i], "--max") && i + 1 < argc) rec_max = atof(argv[++i]);
        else if (!strcmp(argv[i], "--dev") && i + 1 < argc) dev = argv[++i];
    }

    signal(SIGINT, on_sig); signal(SIGTERM, on_sig);

    if (alsa_load()) return 2;
    snd_pcm_t *cap;
    int err = p_snd_pcm_open(&cap, dev, PCM_CAPTURE, 0);
    if (err) { fprintf(stderr, "[xz_capture] open %s: %s\n", dev, p_snd_strerror(err)); return 2; }
    int plain = !!getenv("XZ_HW_PLAIN");
    snd_pcm_hw_params_t *hw;
    if (p_snd_pcm_hw_params_malloc(&hw)) return 2;
    p_snd_pcm_hw_params_any(cap, hw);
    if ((err = p_snd_pcm_hw_params_set_access(cap, hw, ACC_RW_INTERLEAVED)) ||
        (err = p_snd_pcm_hw_params_set_format(cap, hw, FMT_S16)) ||
        (err = p_snd_pcm_hw_params_set_channels(cap, hw, 1))) {
        fprintf(stderr, "[xz_capture] hw set: %s\n", p_snd_strerror(err)); return 2;
    }
    if ((err = p_snd_pcm_hw_params_set_rate(cap, hw, RATE, 0))) {
        fprintf(stderr, "[xz_capture] rate: %s\n", p_snd_strerror(err)); return 2;
    }
    if (!plain) {
        unsigned long ps = FRAME_SAMPLES;
        unsigned int per = 4;
        if ((err = p_snd_pcm_hw_params_set_period_size(cap, hw, ps, 0)) ||
            (err = p_snd_pcm_hw_params_set_periods(cap, hw, per, 0))) {
            fprintf(stderr, "[xz_capture] period: %s\n", p_snd_strerror(err)); return 2;
        }
    }
    if ((err = p_snd_pcm_hw_params(cap, hw))) {
        fprintf(stderr, "[xz_capture] hw config: %s\n", p_snd_strerror(err)); return 2;
    }
    if ((err = p_snd_pcm_prepare(cap))) {
        fprintf(stderr, "[xz_capture] prepare: %s\n", p_snd_strerror(err)); return 2;
    }

    int16_t *frame = malloc(FRAME_SAMPLES * sizeof(int16_t));
    size_t cap_frames = 0, cap_cap = (size_t)RATE * (size_t)(wait_max + rec_max + 2);
    int16_t *pcm = malloc(cap_cap * sizeof(int16_t));
    if (!frame || !pcm) return 2;

    double noise_db = -75.0;  /* 底噪初值, 静音期自适应上浮 */
    double t0 = now_s();

    /* 上电预热: 丢前 5 帧 (设备爆音/直流), 再用 20 帧测环境底噪.
       用中位数: USB 卡偶发满幅垃圾帧, 取 max 会把门限毒化到永远听不见人声 */
    for (int i = 0; i < 5; i++) p_snd_pcm_readi(cap, frame, FRAME_SAMPLES);
    double dbs[20];
    for (int i = 0; i < 20; i++) {
        snd_pcm_sframes_t n = p_snd_pcm_readi(cap, frame, FRAME_SAMPLES);
        dbs[i] = n > 0 ? frame_db(frame, (int)n) : -120.0;
        if (getenv("XZ_CAPTURE_DEBUG") && i < 3) {
            unsigned char *b = (unsigned char *)frame;
            fprintf(stderr, "[dbg] frame%d db=%.1f bytes:", i, dbs[i]);
            for (int k = 0; k < 24; k++) fprintf(stderr, " %02x", b[k]);
            fprintf(stderr, "\n");
        }
    }
    for (int i = 1; i < 20; i++) {  /* 插入排序 */
        double v = dbs[i]; int j = i - 1;
        while (j >= 0 && dbs[j] > v) { dbs[j + 1] = dbs[j]; j--; }
        dbs[j + 1] = v;
    }
    noise_db = fmax(noise_db, dbs[10]);  /* 中位数 */
    fprintf(stderr, "[xz_capture] ambient=%.1f dB (median of 20), voice gate=%.1f dB\n",
            noise_db, noise_db + margin);

    /* 阶段 1: 等待说话 */
    while (!got_sig) {
        snd_pcm_sframes_t n = p_snd_pcm_readi(cap, frame, FRAME_SAMPLES);
        if (n == -EPIPE) { p_snd_pcm_prepare(cap); continue; }
        if (n < 0) { fprintf(stderr, "[xz_capture] read: %s\n", p_snd_strerror((int)n)); return 2; }
        double db = frame_db(frame, FRAME_SAMPLES);
        if (getenv("XZ_CAPTURE_DEBUG")) {
            short m = 0;
            for (int k = 0; k < FRAME_SAMPLES; k++) if (abs(frame[k]) > m) m = abs(frame[k]);
            static int dbg_n = 0;
            if (dbg_n++ < 5) fprintf(stderr, "[dbg] rd=%d maxabs=%d db=%.1f\n", dbg_n, m, db);
        }
        double voice_db = noise_db + margin;
        if (db >= voice_db) {
            memcpy(pcm + cap_frames, frame, (size_t)FRAME_SAMPLES * 2);
            cap_frames += FRAME_SAMPLES;
            break;
        }
        /* 静音帧: 缓慢上浮底噪估计 (只升不降, 免得说话中间被拉低) */
        if (db > noise_db) noise_db = noise_db * 0.95 + db * 0.05;
        if (now_s() - t0 >= wait_max) {
            fprintf(stderr, "[xz_capture] %.1fs 没检测到说话 (noise=%.1f dB)\n",
                    wait_max, noise_db);
            FILE *f = fopen(out_path, "wb");
            if (f) { write_wav(f, pcm, cap_frames); fclose(f);
                     fprintf(stderr, "[xz_capture] 超时也写出 %zu 帧 (诊断用)\n", cap_frames); }
            p_snd_pcm_close(cap);
            return 4;
        }
    }
    double t_voice = now_s();

    /* 阶段 2: 收音直到说完 (trailing 静音) 或录满 */
    double voice_db = noise_db + margin;
    int quiet_frames = 0, voice_frames = 1;
    while (!got_sig && (now_s() - t_voice) < rec_max) {
        snd_pcm_sframes_t n = p_snd_pcm_readi(cap, frame, FRAME_SAMPLES);
        if (n == -EPIPE) { p_snd_pcm_prepare(cap); continue; }
        if (n < 0) break;
        memcpy(pcm + cap_frames, frame, (size_t)FRAME_SAMPLES * 2);
        cap_frames += FRAME_SAMPLES;
        double db = frame_db(frame, FRAME_SAMPLES);
        if (db >= voice_db) { voice_frames++; quiet_frames = 0; }
        else if (++quiet_frames * FRAME_MS / 1000.0 >= trailing_s) break;
    }
    double t_end = now_s();
    p_snd_pcm_drop(cap);
    p_snd_pcm_close(cap);

    if (voice_frames < min_voice_frames) {
        fprintf(stderr, "[xz_capture] 人声太短 (%d 帧), 当作误触发\n", voice_frames);
        return 4;
    }

    FILE *f = fopen(out_path, "wb");
    if (!f) { perror(out_path); return 2; }
    write_wav(f, pcm, cap_frames);
    fclose(f);
    fprintf(stderr,
            "[xz_capture] armed=%.2fs rec=%.2fs total=%.2fs frames=%zu (%.2fs audio) noise=%.1f dB out=%s\n",
            t_voice - t0, t_end - t_voice, t_end - t0,
            cap_frames, (double)cap_frames / RATE, noise_db, out_path);
    free(frame); free(pcm);
    return 0;
}
