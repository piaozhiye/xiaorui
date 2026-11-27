#define _POSIX_C_SOURCE 200809L
#include "audio_player.h"
#include "audio_mix.h"
#include "xr_volume.h"
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
#include <unistd.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/wait.h>
typedef long snd_pcm_sframes_t;
#define A_RW 3
#define A_S16 2
static xr_audio_output_ops_t output;
/* ---------- opus 解码: 板上默认静态链(系统 libopus.so.0 的 decoder 与
   rkaudio_vqe 插件共存时实测写坏内存→进程堆损坏/段错误, 2026-10-14 阶段3
   定位); 主机测试用 -DXR_PLAYER_USE_DLOPEN_OPUS=1 走 dlopen ---------- */
#if XR_PLAYER_USE_DLOPEN_OPUS
#include <dlfcn.h>
/* 无 opus 头的主机测试路径: 最小类型替身(与 ABI 兼容的指针/整型) */
typedef struct OpusDecoder OpusDecoder;
typedef short opus_int16;
static void *opus_lib;
static OpusDecoder *(*o_dec_create)(int, int, int *);
static int (*o_dec_decode)(OpusDecoder *, const unsigned char *, int,
                           short *, int, int);
static int opus_load(void) {
    opus_lib = dlopen("libopus.so.0", RTLD_NOW);
    if (!opus_lib) { fprintf(stderr, "[xr] dlopen libopus: %s\n", dlerror()); return -1; }
    struct { const char *n; void **p; } t[] = {
        {"opus_decoder_create", (void**)&o_dec_create},
        {"opus_decode", (void**)&o_dec_decode},
    };
    for (size_t i = 0; i < sizeof(t)/sizeof(t[0]); i++) {
        *t[i].p = dlsym(opus_lib, t[i].n);
        if (!*t[i].p) { fprintf(stderr, "[xr] dlsym %s: %s\n", t[i].n, dlerror()); return -1; }
    }
    return 0;
}
#else
#include <opus.h>
#define o_dec_create(R, C, E) opus_decoder_create(R, C, E)
#define o_dec_decode(D, P, L, O, OC, F) opus_decode(D, P, L, O, OC, F)
static int opus_load(void) { return 0; }
#endif
static OpusDecoder *decoder;
/* ---------- 播放: 独立线程 ----------
 * 主线程: 收 ws opus 包 -> 解码 PCM -> 写入环形缓冲 (无阻塞, 网络抖动被环吸收)
 * 播放线程: 环形缓冲 -> snd_pcm_writei (卡满时阻塞等待, 自然节拍)
 * 好处: ws 收包停顿不再直接饿死声卡; writei 的阻塞不再拖慢网络读.
 * 环 30s @24k mono 16bit = 720000 样本(1.4MB)。
 * ★ 为什么 30s(2026-11-19 板端"有时候没听到"事故): 网关 3 倍速下发
 *   (tts_audio_send_delay=20)时, 缓冲水位按 (3-1)x 涨 —— 长回答(story
 *   类 20~30s 音频)会涨到 ~2/3 时长, 8s 旧环 + 1.9s ALSA 扛不住 ->
 *   溢出覆盖最旧数据 -> 整段话被无声冲掉。30s 环 + 3x 下发可安全
 *   吃下 ~48s 连续音频(0.5B LLM 的回答远达不到)。 */
#define RING_SAMPLES (24000 * 30)
#define MUSIC_RING_SAMPLES (24000 * 2)
static snd_pcm_t *spk;
static int16_t ring[RING_SAMPLES];
static int16_t music_ring[MUSIC_RING_SAMPLES];
static size_t ring_w, ring_r;            /* 语音写/读指针 (样本) */
static size_t music_w, music_r;
static atomic_int ring_eof;              /* 主线程置 1: 数据发完了, 播完即收尾 */
static atomic_int music_active;           /* 音乐会话在位(FIFO -> 播放环); 见 xr_player_set_music */
static atomic_int music_ducked;            /* KWS/语音期间压低音乐, 仍继续播放并混音 */
static int music_prerolled;                /* 本轮音乐是否已完成预滚 */
static int music_yielded;                  /* 本轮语音是否已让音乐让出过声卡 */
static atomic_int player_running;
/* 主音量(百分比, -1=未设置直通): 同时作用于混音后的音乐与语音。
   独立语音增益另见 voice_gain_pct。换值在下一批 writei 生效(≤120ms)。
   板上喇叭节点无稳定 mixer 控制, 数字增益与声卡无关、可持久化、主机可测。 */
static atomic_int volume_pct = -1;
static atomic_int voice_gain_pct = 100;
/* 诊断开关(默认关, 环境变量打开): XR_PLAY_DUMP=路径.pcm 转储写卡内容,
 * XR_PLAY_TRACE=路径.txt 记录每次 writei 耗时、环余量和音乐 ALSA delay。
 * 用途: 区分"下发的数据本身有断点"与"设备端写卡节奏有问题"。 */
static FILE *play_dump;
static FILE *play_trace;
/* ★ 容量上限(2026-11-20 根分区 99% 事故): 两个诊断文件都是追加写,
 * play.pcm 长跑到 57MB 且默认落 /data(根分区)。规则: 累计达到
 * XR_PLAY_DUMP_MAX_MB / XR_PLAY_TRACE_MAX_MB(默认 32MB)即重置重写,
 * 保留最近内容(诊断要的恰恰是最近一段); 打开时已超限同样重置。
 * MAX_MB=0 恢复无上限旧行为。 */
static char play_dump_path[512], play_trace_path[512];
static long play_dump_bytes, play_dump_cap;
static long play_trace_bytes, play_trace_cap;
static FILE *diag_append_capped(const char *path, const char *max_mb,
                                char *path_out, size_t path_len,
                                long *bytes, long *cap) {
    double mb = 32.0;
    if (max_mb && *max_mb) {
        mb = strtod(max_mb, NULL);
        if (mb < 0) mb = 0;
    }
    *cap = (long)(mb * 1024.0 * 1024.0);
    FILE *f = fopen(path, "ab");
    if (!f) return NULL;
    snprintf(path_out, path_len, "%s", path);
    if (fseek(f, 0, SEEK_END) == 0) {
        *bytes = ftell(f);
        if (*cap > 0 && *bytes >= *cap) {
            fclose(f);
            f = fopen(path, "wb");   /* 打开时已超限: 当场重置 */
            *bytes = 0;
        }
    } else {
        *bytes = 0;
    }
    return f;
}
static void diag_rotate_if_full(FILE **f, const char *path,
                                long *bytes, long cap) {
    if (!*f || cap <= 0 || *bytes < cap) return;
    fclose(*f);
    *f = fopen(path, "wb");
    *bytes = 0;
}
static pthread_mutex_t ring_mu = PTHREAD_MUTEX_INITIALIZER;
/* ★ 等待时钟(Codex review P2, 2026-11-20): 本板无 RTC, 校时/重启会跳
 *   墙钟; grace 的绝对 deadline 若按 CLOCK_REALTIME 构造, 回拨会把已
 *   提交的超时无限拉长(注入 +1h 后尾巴排空测试 assert(flushed) 失败)。
 *   条件变量与 deadline 统一 CLOCK_MONOTONIC —— 只改 clock_gettime
 *   不改条件变量的时钟是错的: condattr 决定内核用哪个钟比较 deadline。
 *   pthread_once 惰性初始化: 触碰 ring_cv 的入口(push/播放线程/finish/
 *   drop)都先 ensure, 语义上等价于"开线程前初始化一次", 且不存在
 *   "仍有等待者时重建"的窗口。 */
static pthread_cond_t ring_cv;
static pthread_once_t ring_cv_once = PTHREAD_ONCE_INIT;
static void ring_cv_init_mono(void) {
    pthread_condattr_t ca;
    pthread_condattr_init(&ca);
    pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
    pthread_cond_init(&ring_cv, &ca);
    pthread_condattr_destroy(&ca);
}
static void ring_cv_ensure(void) { pthread_once(&ring_cv_once, ring_cv_init_mono); }
/* ALSA period 样本数(写卡最小单位)。spk_open 设一次。
 * tmp 缓冲、hw 参数、环消费粒度全用它: 改 period 必须同步改
 * player_thread 里的 tmp[SPK_PERIOD_SAMPLES](独立写死会静默溢栈)。 */
#define SPK_PERIOD_SAMPLES 2880    /* 24kHz 下 120ms */
static size_t spk_period_samples = SPK_PERIOD_SAMPLES;
#define MUSIC_QUEUE_TARGET_FRAMES (250L * 24L) /* 250ms in ALSA client-rate frames */

size_t xr_player_queued_samples(void) {
    size_t w, r;
    pthread_mutex_lock(&ring_mu); w = ring_w; r = ring_r; pthread_mutex_unlock(&ring_mu);
    return w >= r ? w - r : RING_SAMPLES - r + w;
}
size_t xr_player_music_queued_samples(void) {
    size_t w, r;
    pthread_mutex_lock(&ring_mu); w = music_w; r = music_r; pthread_mutex_unlock(&ring_mu);
    return w >= r ? w - r : MUSIC_RING_SAMPLES - r + w;
}

int xr_player_music_duck_percent(void) {
    const char *raw = getenv("XR_MUSIC_DUCK_GAIN_PERCENT");
    if (!raw || !*raw) return 25;
    char *end = NULL;
    long value = strtol(raw, &end, 10);
    if (end == raw || *end || value < 0 || value > 100) return 25;
    return (int)value;
}

static void ring_push_locked(const int16_t *pcm, size_t n) {
    for (size_t i = 0; i < n; i++) {
        ring[ring_w] = pcm[i];
        ring_w = (ring_w + 1) % RING_SAMPLES;
        if (ring_w == ring_r) ring_r = (ring_r + 1) % RING_SAMPLES;
    }
}
/* 语音 PCM 入环。让位由前端按回合显式调用 xr_player_voice_begin/end 控制,
 * 这里只负责写环(保持通用语义: 旧的"音乐/语音共用 push"测试不受影响)。 */
void xr_player_push_pcm(const int16_t *pcm, size_t n) {
    ring_cv_ensure();
    pthread_mutex_lock(&ring_mu);
    ring_push_locked(pcm, n);
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
}
/* 音乐 PCM 入独立环(FIFO 线程专用)。语音期间也持续消费 Snapcast 数据，
 * 播放线程按同一 ALSA 时钟混音；环满时丢最旧帧，避免追播过期音乐。 */
void xr_player_push_music_pcm(const int16_t *pcm, size_t n) {
    if (!pcm || !n || !atomic_load(&music_active)) return;
    ring_cv_ensure();
    pthread_mutex_lock(&ring_mu);
    if (n >= MUSIC_RING_SAMPLES) {
        pcm += n - MUSIC_RING_SAMPLES;
        n = MUSIC_RING_SAMPLES;
        music_r = music_w = 0;
    }
    for (size_t i = 0; i < n; ++i) {
        music_ring[music_w] = pcm[i];
        music_w = (music_w + 1) % MUSIC_RING_SAMPLES;
        if (music_w == music_r) music_r = (music_r + 1) % MUSIC_RING_SAMPLES;
    }
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
}
/* KWS/语音开始时只 duck 本机音乐，不停 Snapclient、不清音乐队列。 */
void xr_player_voice_begin(void) {
    if (!atomic_load(&music_active)) return;
    if (atomic_exchange(&music_ducked, 1)) return;
    fprintf(stderr, "[xr] mixer: 语音/KWS 开始，本机音乐 duck 到 %d%% 并继续混音\n",
            xr_player_music_duck_percent());
}
/* 回合结束: 给尾部 TTS 最多 300ms 排空，再恢复音乐音量。 */
void xr_player_voice_end(void) {
    if (!atomic_load(&music_active)) return;
    if (!atomic_load(&music_ducked)) return;
    for (int i = 0; i < 30; i++) {
        pthread_mutex_lock(&ring_mu);
        size_t q = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
        pthread_mutex_unlock(&ring_mu);
        if (q == 0) break;
        struct timespec d = {.tv_nsec = 10000000};
        nanosleep(&d, NULL);
    }
    ring_cv_ensure();
    pthread_mutex_lock(&ring_mu);
    if (!atomic_load(&music_ducked)) { pthread_mutex_unlock(&ring_mu); return; }
    size_t leftover = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
    if (leftover) ring_w = ring_r = 0;
    atomic_store(&music_ducked, 0);
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
    if (leftover)
        fprintf(stderr, "[xr] mixer: 未排空的语音尾 %zu 样本已清\n", leftover);
    fprintf(stderr, "[xr] mixer: 语音结束，Snapcast 音乐恢复原音量\n");
}
int xr_player_music_ducked(void) { return atomic_load(&music_ducked); }

static int spk_open(void) {
    /* ALSA period 大小(2880 样本 @24kHz = 120ms): 决定单次写卡落下的
     * "垫子"深度。注意这不是"流能否启动"的门槛 —— hw_params 装的默认
     * sw 参数 start_threshold=1, 任何大小的写都能启动流(内核
     * sound/core/pcm_native.c 默认 sw params, 本文件从未调 sw_params)。
     * 凑整 period 写的真正收益见 player_thread 里的根因注释。 */
    const unsigned long spk_period = SPK_PERIOD_SAMPLES;
    spk_period_samples = spk_period;
    /* 预开常驻: 已有句柄直接复用。prepare 失败必须上报, 顺带丢掉僵尸句柄 ——
       否则调用方以为声卡就绪, 播放线程对未 prepare 的句柄写必然出错,
       而句柄还留在 spk 上, 下次又走这条复用分支, 播放永久不可用。 */
    if (spk) {
        int rc = output.a_prepare(spk);
        if (rc) {
            fprintf(stderr, "[xr] spk prepare: %s\n", output.a_strerror(rc));
            output.a_close(spk); spk = NULL;   /* 失败必须关: 僵尸句柄后续永远失败 */
        }
        return rc ? -1 : 0;
    }
    /* 播放设备名走 XR_SPK(默认 xr_spk): RK3576 的 /etc/asound.conf 里有 xr_spk
       (plug → hw:0,0 @48k/2ch), RK3308 的系统配置没有, 由平台补一个同名别名
       (见 ports/rk3308)。核心代码不认平台。 */
    const char *spk_name = getenv("XR_SPK");
    if (!spk_name || !*spk_name) spk_name = "xr_spk";
    if (output.a_open(&spk, spk_name, 0 /*playback*/, 0)) {
        fprintf(stderr, "[xr] spk open: %s\n", output.a_strerror(errno));
        return -1;
    }
    void *hw; output.a_hwp_malloc(&hw); output.a_hwp_any(spk, hw);
    int e = output.a_set_access(spk, hw, A_RW)
         || output.a_set_format(spk, hw, A_S16)
         || output.a_set_channels(spk, hw, 1)
         || output.a_set_rate(spk, hw, 24000, 0)
         || output.a_set_ps(spk, hw, spk_period, 0)      /* 120ms/period */
         /* 20 period = 2.4s, 对齐网关 tts_lead_ms(2s): 卡深度 >= lead,
          * 句间隙由卡内余量整段吸收(实机 live7: 卡满阻塞写, 0 击穿)。
          * 注意: 24 period 被此卡/plug 拒绝(spk hw: Operation not
          * permitted, 2026-11-19 实测), 20 是实证上限内的取值。
          * 加深卡缓冲在 EPIPE 时代是负资产(prepare 丢整卡), 但现在
          * EPIPE 后是 prepare+原数据重写, 不会多丢 —— 加深只剩收益。 */
         || output.a_set_periods(spk, hw, 20, 0);
    if (!e) e = output.a_hwp(spk, hw);
    output.a_hwp_free(hw);
    if (e) {
        fprintf(stderr, "[xr] spk hw: %s\n", output.a_strerror(e));
        output.a_close(spk); spk = NULL;   /* 失败必须关: 泄漏句柄会让后续全 busy */
        return -1;
    }
    if (output.a_prepare(spk)) {
        fprintf(stderr, "[xr] spk prepare: %s\n", output.a_strerror(errno));
        output.a_close(spk); spk = NULL;   /* 同上: 冷态这条失败路径曾泄漏句柄 */
        return -1;
    }
    return 0;
}
/* 播放线程: 从环形缓冲读 PCM 写卡; eof 且环空时 drain 收尾退出 */
static void *player_thread(void *arg) {
    (void)arg;
    ring_cv_ensure();
    int16_t tmp[SPK_PERIOD_SAMPLES];   /* 120ms 一批, 与 ALSA period 同源 */
    int pcm_active = 0;
    {
        const char *dp = getenv("XR_PLAY_DUMP");
        if (dp && *dp) play_dump = diag_append_capped(
            dp, getenv("XR_PLAY_DUMP_MAX_MB"), play_dump_path,
            sizeof(play_dump_path), &play_dump_bytes, &play_dump_cap);
        const char *tp = getenv("XR_PLAY_TRACE");
        if (tp && *tp) {
            play_trace = diag_append_capped(
                tp, getenv("XR_PLAY_TRACE_MAX_MB"), play_trace_path,
                sizeof(play_trace_path), &play_trace_bytes, &play_trace_cap);
            /* 锚点: 把后面的 CLOCK_MONOTONIC 时间戳对齐到墙钟,
             * 这样停顿能直接和网关日志的轮次/合成时刻对上。
             * (这里故意读墙钟——只是日志锚点; 等待 deadline
             * 一律 CLOCK_MONOTONIC, 见 ring_cv_init_mono。) */
            if (play_trace) {
                struct timespec rt;
                clock_gettime(CLOCK_REALTIME, &rt);
                fprintf(play_trace, "# rt=%ld.%03ld\n",
                        (long)rt.tv_sec, (long)(rt.tv_nsec / 1000000));
                fflush(play_trace);
            }
        }
    }
    int16_t music_tmp[SPK_PERIOD_SAMPLES], voice_tmp[SPK_PERIOD_SAMPLES];
    xr_audio_gain_ramp_t music_gain;
    xr_audio_gain_ramp_init(&music_gain, 100);
    while (player_running) {
        pthread_mutex_lock(&ring_mu);
        size_t voice_avail = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
        size_t music_avail = music_w >= music_r ? music_w - music_r : MUSIC_RING_SAMPLES - music_r + music_w;
        int music_on = atomic_load(&music_active);
        while (voice_avail == 0 && (!music_on || music_avail == 0) &&
               !ring_eof && player_running) {
            if (pcm_active) {
                /* 短暂无包时保留 ALSA 队列 300ms；真正空闲才 drain/prepare。 */
                struct timespec ts;
                clock_gettime(CLOCK_MONOTONIC, &ts);
                ts.tv_nsec += 300L * 1000000L;
                if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
                int rc = pthread_cond_timedwait(&ring_cv, &ring_mu, &ts);
                voice_avail = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
                music_avail = music_w >= music_r ? music_w - music_r : MUSIC_RING_SAMPLES - music_r + music_w;
                music_on = atomic_load(&music_active);
                if (rc != ETIMEDOUT) continue;
                if (voice_avail || (music_on && music_avail) || ring_eof || !player_running) continue;
                pthread_mutex_unlock(&ring_mu);
                int drc = output.a_drain(spk);
                if (drc < 0) fprintf(stderr, "[xr] spk idle drain: %s\n", output.a_strerror(drc));
                if (player_running && output.a_prepare(spk) < 0)
                    fprintf(stderr, "[xr] spk idle prepare failed\n");
                pcm_active = 0;
                pthread_mutex_lock(&ring_mu);
                if (atomic_load(&music_active)) music_prerolled = 0;
                voice_avail = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
                music_avail = music_w >= music_r ? music_w - music_r : MUSIC_RING_SAMPLES - music_r + music_w;
                music_on = atomic_load(&music_active);
            }
            if (voice_avail || (music_on && music_avail) || ring_eof || !player_running) break;
            pthread_cond_wait(&ring_cv, &ring_mu);
            voice_avail = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
            music_avail = music_w >= music_r ? music_w - music_r : MUSIC_RING_SAMPLES - music_r + music_w;
            music_on = atomic_load(&music_active);
        }
        if (!player_running || (voice_avail == 0 && !music_on && ring_eof)) {
            pthread_mutex_unlock(&ring_mu);
            break;
        }

        /* 音乐预滚只等音乐数据；语音先到时立即播语音，不被音乐启动门槛挡住。 */
        if (music_on && music_avail && !music_prerolled && voice_avail == 0) {
            size_t target = spk_period_samples * 2;
            if (music_avail < target) {
                struct timespec ts;
                clock_gettime(CLOCK_MONOTONIC, &ts);
                ts.tv_nsec += 50L * 1000000L;
                if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
                pthread_cond_timedwait(&ring_cv, &ring_mu, &ts);
                pthread_mutex_unlock(&ring_mu);
                continue;
            }
            music_prerolled = 1;
            fprintf(stderr, "[xr] music preroll: %zu 样本 (~%.0f ms)\n",
                    music_avail, (double)music_avail / 24.0);
        }

        /* TTS-only retains the existing period batching. It avoids feeding
         * tiny network fragments directly to ALSA; a final short tail is
         * released after 300ms or immediately at EOF. */
        if (!music_on && voice_avail > 0 && voice_avail < spk_period_samples && !ring_eof) {
            int tail_hold_ms = 0;
            while (voice_avail < spk_period_samples && !ring_eof && player_running) {
                struct timespec ts;
                clock_gettime(CLOCK_MONOTONIC, &ts);
                ts.tv_nsec += 50L * 1000000L;
                if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
                int rc = pthread_cond_timedwait(&ring_cv, &ring_mu, &ts);
                voice_avail = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
                if (rc == ETIMEDOUT && voice_avail < spk_period_samples) {
                    tail_hold_ms += 50;
                    if (tail_hold_ms < 300) continue;
                    pthread_mutex_unlock(&ring_mu);
                    (void)output.a_drain(spk);
                    if (player_running && output.a_prepare(spk) < 0)
                        fprintf(stderr, "[xr] spk tail prepare failed\n");
                    pcm_active = 0;
                    pthread_mutex_lock(&ring_mu);
                    voice_avail = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
                    break;
                }
            }
            if (!player_running) { pthread_mutex_unlock(&ring_mu); break; }
        }

        long music_delay_frames = -1;
        if (music_on && music_avail && spk && output.a_delay) {
            long q = 0;
            if (output.a_delay(spk, &q) == 0) {
                music_delay_frames = q;
                if (q < 0) q = 0;
                long total_before = q + (long)music_avail;
                long excess = total_before - MUSIC_QUEUE_TARGET_FRAMES;
                if (excess > 0 && music_avail > 0) {
                    size_t discard = (size_t)excess < music_avail ? (size_t)excess : music_avail;
                    music_r = (music_r + discard) % MUSIC_RING_SAMPLES;
                    music_avail -= discard;
                    if (getenv("XR_MUSIC_TRACE")) {
                        struct timespec ts;
                        clock_gettime(CLOCK_MONOTONIC, &ts);
                        fprintf(stderr,
                                "[xr] music_catchup %ld.%06ld q=%ld drop=%zu music_ring=%zu total_before=%ld target=%ld\n",
                                (long)ts.tv_sec, (long)(ts.tv_nsec / 1000), q,
                                discard, music_avail, total_before,
                                (long)MUSIC_QUEUE_TARGET_FRAMES);
                    }
                }
            }
        }

        /* Snapcast 是主时钟：有音乐帧时按音乐帧数推进；音乐短暂断流时
         * 允许 TTS 独立推进。两个源各自出队后在同一批 ALSA 帧上求和。 */
        size_t available = (music_on && music_avail) ? music_avail : voice_avail;
        size_t take = available < spk_period_samples ? available : spk_period_samples;
        if (take == 0) {
            pthread_mutex_unlock(&ring_mu);
            continue;
        }
        size_t voice_take = voice_avail < take ? voice_avail : take;
        size_t music_take = (music_on && music_avail) ? take : 0;
        for (size_t i = 0; i < take; ++i) {
            voice_tmp[i] = 0;
            music_tmp[i] = 0;
        }
        for (size_t i = 0; i < voice_take; ++i) {
            voice_tmp[i] = ring[ring_r];
            ring_r = (ring_r + 1) % RING_SAMPLES;
        }
        for (size_t i = 0; i < music_take; ++i) {
            music_tmp[i] = music_ring[music_r];
            music_r = (music_r + 1) % MUSIC_RING_SAMPLES;
        }
        pthread_mutex_unlock(&ring_mu);

        int voice_gain = atomic_load(&voice_gain_pct);
        if (voice_gain < 100) xr_volume_apply(voice_tmp, voice_take, voice_gain);

        int music_gain_percent = (music_on && atomic_load(&music_ducked))
                                 ? xr_player_music_duck_percent() : 100;
        xr_audio_mix_s16_ramped(tmp, music_tmp, voice_tmp, take,
                                music_on ? music_gain_percent : 0,
                                480 /* 20ms @ 24kHz */, &music_gain);
        int vpct = atomic_load(&volume_pct);
        if (vpct > 0 && vpct < 100) xr_volume_apply(tmp, take, vpct);
        if (play_dump) {   /* 诊断: 实际写卡前的内容(耳机里应当听到的就是它) */
            fwrite(tmp, 2, take, play_dump);
            fflush(play_dump);
            play_dump_bytes += (long)(take * 2);
            diag_rotate_if_full(&play_dump, play_dump_path,
                                &play_dump_bytes, play_dump_cap);
        }
        size_t written = 0;
        while (written < take && player_running) {
            struct timespec t0, t1;
            clock_gettime(CLOCK_MONOTONIC, &t0);
            snd_pcm_sframes_t wn = output.a_writei(spk, tmp + written, (unsigned long)(take - written));
            clock_gettime(CLOCK_MONOTONIC, &t1);
            if (play_trace) {  /* 诊断: 写卡耗时(阻塞=卡满, 长时间=被饿住) */
                double ms = (t1.tv_sec - t0.tv_sec) * 1000.0 +
                            (t1.tv_nsec - t0.tv_nsec) / 1e6;
                int n = fprintf(play_trace, "%.3f %ld %ld %.1f %zu %ld\n",
                        (t1.tv_sec % 100000) + t1.tv_nsec / 1e9,
                        (long)written, (long)wn, ms, available, music_delay_frames);
                /* 必须 flush: 不 flush 时 trace 落后 stdio 缓冲几个 KB,
                 * 正在播的轮次在文件里看着像"只播了一半"(2026-11-19 误判)。 */
                fflush(play_trace);
                if (n > 0) play_trace_bytes += n;
                diag_rotate_if_full(&play_trace, play_trace_path,
                                    &play_trace_bytes, play_trace_cap);
            }
            if (wn == -EINTR) continue;
            if (wn == -EPIPE) {
                fprintf(stderr, "[xr] spk underrun, prepare\n");
                if (output.a_prepare(spk) >= 0) continue;
            }
            if (wn <= 0) {
                fprintf(stderr, "[xr] spk writei: %s\n", wn < 0 ? output.a_strerror((int)wn) : "zero frames");
                player_running = 0;
                break;
            }
            written += (size_t)wn;
            pcm_active = 1;
        }
    }
    if (spk) {
        if (player_running) output.a_drain(spk);
        else output.a_drop(spk);
        output.a_drop(spk);   /* 常驻不关 */
    }
    if (play_dump) { fclose(play_dump); play_dump = NULL; }
    if (play_trace) { fclose(play_trace); play_trace = NULL; }
    player_running = 0;
    return NULL;
}

/* 主线程调: 开卡 + 起播放线程 + 清环 */
static pthread_t player_tid;
static int player_active;                /* 线程存活标记, 防双开泄漏声卡 */

void xr_player_set_music(int on) {
    atomic_store(&music_active, on ? 1 : 0);
    ring_cv_ensure();
    pthread_mutex_lock(&ring_mu);
    if (!on) {
        music_prerolled = 0;
        music_w = music_r = 0;            /* 新会话不能追播旧 FIFO 队列 */
        atomic_store(&music_ducked, 0);
    }
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
}
int xr_player_music_active(void) { return atomic_load(&music_active); }
void xr_player_finish(void);
void xr_player_drop(void);
/* 挂起/恢复默认 PA sink。snapclient 经 PulseAudio 出声, PA 的
 * module-suspend-on-idle 默认 ~5s 才放 hw; 显式挂起可压到 ~90ms(实测)。
 * timeout 2: PA 卡死时不能把语音播放一起拖住。
 * XR_PA_AUTO_SUSPEND=1 仅在 default.pa 已配置
 * `load-module module-suspend-on-idle timeout=0` 并重启 PA 后启用: 交给
 * PA 空闲策略自动释放/恢复硬件, 不再每轮 fork pactl。缺配置时保留旧路径。 */
static int pa_auto_suspend_enabled(void) {
    const char *value = getenv("XR_PA_AUTO_SUSPEND");
    return value && atoi(value) > 0;
}
static void pa_sink_suspend(int on) {
    if (pa_auto_suspend_enabled()) return;
    int rc = system(on
        ? "timeout 2 sh -c 'S=$(pactl get-default-sink 2>/dev/null); "
          "[ -n \"$S\" ] && pactl suspend-sink \"$S\" 1' >/dev/null 2>&1"
        : "timeout 2 sh -c 'S=$(pactl get-default-sink 2>/dev/null); "
          "[ -n \"$S\" ] && pactl suspend-sink \"$S\" 0' >/dev/null 2>&1");
    (void)rc;
}
/* 把声卡还给音乐(直占 ALSA 路径)。finish 和 drop(中止/重连)都必须调用,
 * 否则中止一轮后 snapclient 不再起、sink 仍挂起、spk 还占着卡 -> 永久静音。 */
static void music_unyield(void) {
    if (!music_yielded) return;
    music_yielded = 0;
    xr_player_release_device();          /* 先关 spk, 再让 PA/客户端拿卡 */
    pa_sink_suspend(0);
    const char *cmd = getenv("XR_SNAPCLIENT_RESTART");
    if (cmd && *cmd) {
        fprintf(stderr, "[xr] yield: 重启 snapclient\n");
        int rc = system(cmd);
        if (rc == -1) {
            fprintf(stderr, "[xr] yield: 无法执行 snapclient 重启命令\n");
        } else if (!WIFEXITED(rc) || WEXITSTATUS(rc) != 0) {
            fprintf(stderr, "[xr] yield: snapclient 重启命令失败 status=%d\n", rc);
        }
    }
}
/* 语音要出声、而音乐(snapclient)正占着声卡时, 让音乐让位:
 * 停掉 snapclient -> 重试打开声卡。由 XR_MUSIC_YIELD=1 打开, 默认关闭(不改变行为)。
 * 让位后由 xr_player_finish_with_poll() 负责还卡 + 用 XR_SNAPCLIENT_RESTART 重启它。
 */
static int yield_music_if_needed(void) {
    const char *enabled = getenv("XR_MUSIC_YIELD");
    if (!enabled || atoi(enabled) <= 0) return 0;
    /* 不看 music_active: 音乐可能是 snapclient 直出 ALSA(FIFO 线程没跑), 但只要
       有 snapclient 在跑, 它就占着声卡, 语音就得让它让位 */
    int running = system("pgrep -x snapclient >/dev/null 2>&1");
    if (running != 0) return 0;                  /* 没在跑: 不用让 */
    fprintf(stderr, "[xr] yield: 停 snapclient 让出声卡\n");
    { int rc = system("pkill -x snapclient 2>/dev/null"); if (rc < 0) {} }
    /* 从这里开始 snapclient 已被要求退出。无论后续开卡是否成功，失败路径
       都必须经 music_unyield() 恢复 sink/客户端，不能留下永久静音。 */
    music_yielded = 1;
    /* snapclient 默认经 PulseAudio 出声; PA 的 module-suspend-on-idle 默认 ~5s 才
     * 释放 hw(实测 pkill 后 5.1s), 前端 3s 重试必然拿不到卡。显式挂起默认 sink
     * 可把 hw 释放压到 ~90ms(2026-11-24 实测)。没有 pactl 时静默跳过。 */
    pa_sink_suspend(1);
    for (int i = 0; i < 30; i++) {          /* 最多等 3s */
        if (spk_open() == 0) {
            return 1;
        }
        struct timespec d = {.tv_nsec = 100000000};
        nanosleep(&d, NULL);
    }
    fprintf(stderr, "[xr] yield: 3s 内没拿回声卡\n");
    music_unyield();
    return -1;
}

int xr_player_start(void) {
    if (atomic_load(&music_active)) {
        /* 音乐会话在位: 环里有音乐, 不能清; 线程在跑就直接复用 */
        ring_eof = 0;
        ring_cv_ensure();
        pthread_mutex_lock(&ring_mu);
        pthread_cond_broadcast(&ring_cv);
        pthread_mutex_unlock(&ring_mu);
        if (player_active) return 0;
        /* 线程没跑(刚收完一轮语音): 必须走 spk_open() —— 它会复用常驻句柄
         * **并 prepare**。句柄常驻时不能只看 !spk 就跳过, 否则设备还停在
         * SETUP, 线程第一笔 writei 就是 EBADFD(实测踩到)。 */
        if (spk_open()) return -1;
        player_running = 1;
        if (pthread_create(&player_tid, NULL, player_thread, NULL)) {
            player_running = 0; return -1;
        }
        player_active = 1;
        return 0;
    }
    if (player_active) xr_player_finish();  /* 上一轮线程还活着: 先收干净再开新的 */
    if (yield_music_if_needed() < 0) return -1; /* 让位失败已回滚, 不再盲目重试开卡 */
    ring_w = ring_r = 0; ring_eof = 0;
    if (spk_open()) { music_unyield(); return -1; }
    player_running = 1;
    if (pthread_create(&player_tid, NULL, player_thread, NULL)) {
        output.a_drop(spk); player_running = 0;
        music_unyield(); return -1;   /* 常驻句柄; 若曾让位则释放并恢复音乐 */
    }
    player_active = 1;
    return 0;
}
/* 排干收尾 (句尾不截字): 标记 eof, 等线程 drain 完退出 */
void xr_player_finish(void) {
    (void)xr_player_finish_with_poll(NULL, NULL);
}

int xr_player_finish_with_poll(int (*poll_callback)(void *), void *context) {
    if (atomic_load(&music_active)) {
        /* 音乐会话在位: 只表示"这句语音说完了"。置 eof/join 会把音乐一起收掉 */
        return 0;
    }
    if (!player_active) return 0;   /* 以 player_active 判活: 线程可能已因 writei 错自杀,
                                      此时仍需语义复位(player_running 由线程自己清了) */
    ring_eof = 1;
    ring_cv_ensure();
    pthread_mutex_lock(&ring_mu);
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
    int cancelled = 0;
    while (poll_callback && atomic_load(&player_running)) {
        if (poll_callback(context)) {
            player_running = 0;
            cancelled = 1;
            break;
        }
        struct timespec delay = {.tv_nsec = 10000000};
        nanosleep(&delay, NULL);
    }
    pthread_join(player_tid, NULL);   /* join 僵尸线程, 防资源泄漏 */
    player_active = 0; player_running = 0;
    music_unyield();                  /* 直占路径: 还卡 + 恢复 sink + 重启客户端 */
    fprintf(stderr, "[xr] 播放线程收尾 ok\n");
    return cancelled;
}
/* 立即停 (打断/重连): 清 eof 让线程下一轮退出, drop 丢余音 */
void xr_player_drop(void) {
    if (atomic_load(&music_active)) {
        /* 音乐会话在位: 只清软件环余量, 保持 ALSA 流继续运行。
           snd_pcm_drop 会把句柄切到 SETUP, 后续 writei 报 EBADFD 并杀死播放线程。 */
        ring_cv_ensure();
        pthread_mutex_lock(&ring_mu);
        ring_w = ring_r = 0;
        ring_eof = 0;
        pthread_cond_broadcast(&ring_cv);
        pthread_mutex_unlock(&ring_mu);
        return;
    }
    if (!player_active) { music_unyield(); if (spk) output.a_drop(spk); return; }   /* 常驻不关 */
    player_running = 0;
    ring_eof = 1;
    ring_cv_ensure();
    pthread_mutex_lock(&ring_mu);
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
    pthread_join(player_tid, NULL);
    player_active = 0; player_running = 0;
    music_unyield();               /* 中止一轮(打断/断线): 也必须还卡+恢复音乐 */
    if (spk) output.a_drop(spk);   /* 常驻不关 */
}

void xr_player_release_device(void) {
    /* 音乐会话也要一起复位: 释放后由对端(音乐)接管 */
    ring_cv_ensure();
    pthread_mutex_lock(&ring_mu);
    atomic_store(&music_active, 0);
    atomic_store(&music_ducked, 0);
    music_prerolled = 0;
    music_w = music_r = 0;
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
    if (player_active) {
        player_running = 0;
        ring_eof = 1;
        ring_cv_ensure();
        pthread_mutex_lock(&ring_mu);
        pthread_cond_broadcast(&ring_cv);
        pthread_mutex_unlock(&ring_mu);
        pthread_join(player_tid, NULL);
        player_active = 0;
        player_running = 0;
    }
    if (spk) {
        output.a_drain(spk);
        output.a_close(spk);
        spk = NULL;                   /* 下次 xr_player_start 会重新 open */
    }
    fprintf(stderr, "[xr] 声卡已释放(close spk)\n");
}

void xr_player_set_volume(int pct) {
    if (pct < 0) pct = -1;          /* -1/0: 未设置 -> 直通 */
    if (pct > 100) pct = 100;
    atomic_store(&volume_pct, pct);
}

int xr_player_volume(void) { return atomic_load(&volume_pct); }

void xr_player_set_voice_gain(int pct) {
    if (pct < 1) pct = 1;
    if (pct > 100) pct = 100;
    atomic_store(&voice_gain_pct, pct);
}


int xr_player_init(const xr_audio_output_ops_t *ops) {
    output = *ops;
    if (opus_load()) return -1;
    int error = 0;
    decoder = o_dec_create(24000, 1, &error);
    if (!decoder) { fprintf(stderr, "[xr] opus dec %d\n", error); return -1; }
    /* VQE 链(ES8388): 进程内"先采后播"SAI 驱动拒播(hw_params EPERM),
       实测先播后采可行——init 预开播放句柄常驻复用, 不再按轮开关 */
    for (int t = 0; t < 3 && !spk; t++) {   /* SAI 瞬态重试吸收 */
        if (spk_open() == 0) { fprintf(stderr, "[xr] spk 预开常驻(VQE 序规避)\n"); break; }
        sleep(1);
    }
    return 0;
}

int xr_player_push_opus(const unsigned char *packet, size_t size) {
    int16_t pcm[5760];
    if (!decoder || size > 2147483647u) return -1;
    int samples = o_dec_decode(decoder, packet, (int)size, pcm, 5760, 0);
    if (samples > 0) xr_player_push_pcm(pcm, (size_t)samples);
    return samples;
}
