#define _POSIX_C_SOURCE 200809L
#include "audio_player.h"
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
static snd_pcm_t *spk;
static int16_t ring[RING_SAMPLES];
static size_t ring_w, ring_r;            /* 写/读指针 (样本) */
static atomic_int ring_eof;              /* 主线程置 1: 数据发完了, 播完即收尾 */
static atomic_int player_running;
/* 软件音量(百分比, -1=未设置直通): 回答/ack/全部提示音共用这一个旋钮,
   换值在下一批 writei 生效(≤120ms)。板上喇叭节点无稳定 mixer 控制, 数字
   增益与声卡无关、可持久化、主机可测。 */
static atomic_int volume_pct = -1;
/* 诊断开关(默认关, 环境变量打开): XR_PLAY_DUMP=路径.pcm 转储写卡内容,
 * XR_PLAY_TRACE=路径.txt 记录每次 writei 的耗时。
 * 用途: 区分"下发的数据本身有断点"与"设备端写卡节奏有问题"。 */
static FILE *play_dump;
static FILE *play_trace;
static pthread_mutex_t ring_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ring_cv = PTHREAD_COND_INITIALIZER;
/* ALSA period 样本数(写卡最小单位)。spk_open 设一次。
 * tmp 缓冲、hw 参数、环消费粒度全用它: 改 period 必须同步改
 * player_thread 里的 tmp[SPK_PERIOD_SAMPLES](独立写死会静默溢栈)。 */
#define SPK_PERIOD_SAMPLES 2880    /* 24kHz 下 120ms */
static size_t spk_period_samples = SPK_PERIOD_SAMPLES;

size_t xr_player_queued_samples(void) {
    size_t w, r;
    pthread_mutex_lock(&ring_mu); w = ring_w; r = ring_r; pthread_mutex_unlock(&ring_mu);
    return w >= r ? w - r : RING_SAMPLES - r + w;
}

void xr_player_push_pcm(const int16_t *pcm, size_t n) {
    pthread_mutex_lock(&ring_mu);
    for (size_t i = 0; i < n; i++) {
        ring[ring_w] = pcm[i];
        ring_w = (ring_w + 1) % RING_SAMPLES;
        if (ring_w == ring_r) ring_r = (ring_r + 1) % RING_SAMPLES;
    }
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
}

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
    int16_t tmp[SPK_PERIOD_SAMPLES];   /* 120ms 一批, 与 ALSA period 同源 */
    int pcm_active = 0;
    {
        const char *dp = getenv("XR_PLAY_DUMP");
        if (dp && *dp) play_dump = fopen(dp, "ab");
        const char *tp = getenv("XR_PLAY_TRACE");
        if (tp && *tp) {
            play_trace = fopen(tp, "a");
            /* 锚点: 把后面的 CLOCK_MONOTONIC 时间戳对齐到墙钟,
             * 这样停顿能直接和网关日志的轮次/合成时刻对上。 */
            if (play_trace) {
                struct timespec rt;
                clock_gettime(CLOCK_REALTIME, &rt);
                fprintf(play_trace, "# rt=%ld.%03ld\n",
                        (long)rt.tv_sec, (long)(rt.tv_nsec / 1000000));
                fflush(play_trace);
            }
        }
    }
    while (player_running) {
        pthread_mutex_lock(&ring_mu);
        while (ring_w == ring_r && !ring_eof && player_running) {
            if (pcm_active) {
                /* ★ 瞬时空 ≠ 句间空(2026-11-19 板端听感事故):
                 *   环瞬时见空就 drain, 等于把 ALSA 里 ~1.9s 的平滑垫亲手
                 *   放掉再清零, 新数据来了重启声卡 —— 实时下发节奏下环
                 *   常年贴空跑, 主循环每 100~300ms 拉一次包的抖动就触发
                 *   一次"排空-重启", 用户听到句内字级顿挫。
                 *   改法: 先限时等 300ms, 等到数据就不动声卡(ALSA 缓冲
                 *   继续播, 无缝); 真空满 300ms 才是句间空, 那时再排空
                 *   进 PREPARED 防 EPIPE(问候音与下一句可隔数秒)。 */
                struct timespec ts;
                clock_gettime(CLOCK_REALTIME, &ts);
                ts.tv_nsec += 300L * 1000000L;
                if (ts.tv_nsec >= 1000000000L) {
                    ts.tv_sec += 1;
                    ts.tv_nsec -= 1000000000L;
                }
                int rc = pthread_cond_timedwait(&ring_cv, &ring_mu, &ts);
                if (rc != ETIMEDOUT) continue;   /* 等到数据/中断: 重查条件 */
                if (ring_w != ring_r || ring_eof || !player_running) break;
                pthread_mutex_unlock(&ring_mu);
                int drc = output.a_drain(spk);
                if (drc < 0)
                    fprintf(stderr, "[xr] spk idle drain: %s\n",
                            output.a_strerror(drc));
                if (player_running && output.a_prepare(spk) < 0)
                    fprintf(stderr, "[xr] spk idle prepare failed\n");
                pcm_active = 0;
                pthread_mutex_lock(&ring_mu);
            }
            if (ring_w != ring_r || ring_eof || !player_running) break;
            pthread_cond_wait(&ring_cv, &ring_mu);
        }
        size_t avail = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
        if (!player_running || (avail == 0 && ring_eof)) {
            pthread_mutex_unlock(&ring_mu);
            break;
        }
        /* ★ 2026-11-19 板端实机断音根因(2): 按 period 对齐写卡。
         *   机制(2026-11-19 二次 review 对照内核源码核实): 历史上的
         *   "半个 period 写入 -> EPIPE 循环" 并不是"流起不来"——ALSA
         *   默认 sw 参数 start_threshold=1, 1440 样本的写也能启动流。
         *   真正的病灶是**零垫子写节奏**: 每包 opus 只解出 1440(60ms),
         *   写多少卡里就只剩多少, 供给一抖(句间合成空隙)卡立刻干转
         *   XRUN -> prepare 丢余量 -> 又干转, writei 全部 ~0ms +
         *   大量 -EPIPE。
         *   按 period 凑整再写, 每次 writei 都给卡垫进整 120ms 余量,
         *   供给抖动被垫子吸收。
         *   收尾(eof)时按实际有的写, 不能因为凑不满而丢尾巴。 */
        if (avail < spk_period_samples && !ring_eof) {
            /* 继续等下一包, 直到凑够一个 period。 */
            int tail_hold_ms = 0;
            while (avail < spk_period_samples && !ring_eof && player_running) {
                struct timespec ts;
                clock_gettime(CLOCK_REALTIME, &ts);
                ts.tv_nsec += 50L * 1000000L;
                if (ts.tv_nsec >= 1000000000L) {
                    ts.tv_sec += 1;
                    ts.tv_nsec -= 1000000000L;
                }
                int rc = pthread_cond_timedwait(&ring_cv, &ring_mu, &ts);
                avail = ring_w >= ring_r ? ring_w - ring_r : RING_SAMPLES - ring_r + ring_w;
                if (rc == ETIMEDOUT && avail < spk_period_samples) {
                    tail_hold_ms += 50;
                    if (tail_hold_ms < 300) continue;
                    /* ★ 尾巴滞留(2026-11-19 live10 实测): 上一轮余音不是
                     * 整 period(问候音 0.45s=3.76period)会卡住尾巴, 环不空
                     * -> 空环的 300ms 优雅排空不触发 -> 声卡在 RUNNING 态
                     * 播干 -> 下一轮首包凑满 period 写入撞 XRUN(EPIPE)。
                     * 真空满 300ms: 先 drain+prepare 复位流, 再把尾巴写出。
                     * 实际行为(默认 start_threshold=1, 二次 review 核实):
                     * 这个 partial 写入会**立即起播**并在 ~90ms 内播完,
                     * 随后静默 XRUN; 紧接着的空环 grace(pcm_active=1)
                     * 会 drain-from-XRUN + prepare, 给下一轮留下干净的
                     * PREPARED 流。听感无缝: 尾巴在它的自然时刻播掉,
                     * 下一轮数据到达时全新起播。
                     * 两条 grace 路径看着重复, 实际各司其职 —— 即使将来
                     * 显式设了更大的 start_threshold, 内核 drain-init 会
                     * 起播 PREPARED 流里的滞留数据, 之后仍由空环 grace
                     * 收尾。别按"冗余"删掉任何一条。 */
                    pthread_mutex_unlock(&ring_mu);
                    (void)output.a_drain(spk);
                    if (player_running && output.a_prepare(spk) < 0)
                        fprintf(stderr, "[xr] spk tail prepare failed\n");
                    pcm_active = 0;
                    pthread_mutex_lock(&ring_mu);
                    break;
                }
            }
            if (!player_running) { pthread_mutex_unlock(&ring_mu); break; }
            if (avail == 0 && ring_eof) { pthread_mutex_unlock(&ring_mu); break; }
        }
        size_t take = avail < spk_period_samples ? avail : spk_period_samples;
        for (size_t i = 0; i < take; i++) {
            tmp[i] = ring[ring_r];
            ring_r = (ring_r + 1) % RING_SAMPLES;
        }
        pthread_mutex_unlock(&ring_mu);
        int vpct = atomic_load(&volume_pct);
        if (vpct > 0 && vpct < 100) xr_volume_apply(tmp, take, vpct);
        if (play_dump) {   /* 诊断: 实际写卡前的内容(耳机里应当听到的就是它) */
            fwrite(tmp, 2, take, play_dump);
            fflush(play_dump);
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
                fprintf(play_trace, "%.3f %ld %ld %.1f\n",
                        (t1.tv_sec % 100000) + t1.tv_nsec / 1e9,
                        (long)written, (long)wn, ms);
                /* 必须 flush: 不 flush 时 trace 落后 stdio 缓冲几个 KB,
                 * 正在播的轮次在文件里看着像"只播了一半"(2026-11-19 误判)。 */
                fflush(play_trace);
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
void xr_player_finish(void);
void xr_player_drop(void);
int xr_player_start(void) {
    if (player_active) xr_player_finish();  /* 上一轮线程还活着: 先收干净再开新的 */
    ring_w = ring_r = 0; ring_eof = 0;
    if (spk_open()) return -1;
    player_running = 1;
    if (pthread_create(&player_tid, NULL, player_thread, NULL)) {
        output.a_drop(spk); player_running = 0; return -1;   /* 常驻不关 */
    }
    player_active = 1;
    return 0;
}
/* 排干收尾 (句尾不截字): 标记 eof, 等线程 drain 完退出 */
void xr_player_finish(void) {
    (void)xr_player_finish_with_poll(NULL, NULL);
}

int xr_player_finish_with_poll(int (*poll_callback)(void *), void *context) {
    if (!player_active) return 0;   /* 以 player_active 判活: 线程可能已因 writei 错自杀,
                                      此时仍需语义复位(player_running 由线程自己清了) */
    ring_eof = 1;
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
    fprintf(stderr, "[xr] 播放线程收尾 ok\n");
    return cancelled;
}
/* 立即停 (打断/重连): 清 eof 让线程下一轮退出, drop 丢余音 */
void xr_player_drop(void) {
    if (!player_active) { if (spk) output.a_drop(spk); return; }   /* 常驻不关 */
    player_running = 0;
    ring_eof = 1;
    pthread_mutex_lock(&ring_mu);
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
    pthread_join(player_tid, NULL);
    player_active = 0; player_running = 0;
    if (spk) output.a_drop(spk);   /* 常驻不关 */
}

void xr_player_set_volume(int pct) {
    if (pct < 0) pct = -1;          /* -1/0: 未设置 -> 直通 */
    if (pct > 100) pct = 100;
    atomic_store(&volume_pct, pct);
}

int xr_player_volume(void) { return atomic_load(&volume_pct); }


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
