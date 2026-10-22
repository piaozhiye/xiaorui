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
 * 环 8s @24k mono 16bit = 192000 样本(384000 字节). */
#define RING_SAMPLES (24000 * 8)
static snd_pcm_t *spk;
static int16_t ring[RING_SAMPLES];
static size_t ring_w, ring_r;            /* 写/读指针 (样本) */
static atomic_int ring_eof;              /* 主线程置 1: 数据发完了, 播完即收尾 */
static atomic_int player_running;
/* 软件音量(百分比, -1=未设置直通): 回答/ack/全部提示音共用这一个旋钮,
   换值在下一批 writei 生效(≤120ms)。板上喇叭节点无稳定 mixer 控制, 数字
   增益与声卡无关、可持久化、主机可测。 */
static atomic_int volume_pct = -1;
static pthread_mutex_t ring_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ring_cv = PTHREAD_COND_INITIALIZER;

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
         || output.a_set_ps(spk, hw, 2880, 0)      /* 120ms/period */
         || output.a_set_periods(spk, hw, 16, 0);  /* 16 period ~= 1.9s, 抗句间抖动 */
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
    int16_t tmp[2880];              /* 120ms 一批 */
    int pcm_active = 0;
    while (player_running) {
        pthread_mutex_lock(&ring_mu);
        while (ring_w == ring_r && !ring_eof && player_running) {
            /* 问候音与下一句 TTS 之间可间隔数秒。若声卡继续 RUNNING，
             * 队列暂空就会触发 EPIPE；先播完现有数据，再进入 PREPARED。 */
            if (pcm_active) {
                pthread_mutex_unlock(&ring_mu);
                int rc = output.a_drain(spk);
                if (rc < 0) fprintf(stderr, "[xr] spk idle drain: %s\n", output.a_strerror(rc));
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
        size_t take = avail < 2880 ? avail : 2880;
        for (size_t i = 0; i < take; i++) {
            tmp[i] = ring[ring_r];
            ring_r = (ring_r + 1) % RING_SAMPLES;
        }
        pthread_mutex_unlock(&ring_mu);
        int vpct = atomic_load(&volume_pct);
        if (vpct > 0 && vpct < 100) xr_volume_apply(tmp, take, vpct);
        size_t written = 0;
        while (written < take && player_running) {
            snd_pcm_sframes_t wn = output.a_writei(spk, tmp + written, (unsigned long)(take - written));
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
    if (!player_active) return;   /* 以 player_active 判活: 线程可能已因 writei 错自杀,
                                      此时仍需语义复位(player_running 由线程自己清了) */
    ring_eof = 1;
    pthread_mutex_lock(&ring_mu);
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mu);
    pthread_join(player_tid, NULL);   /* join 僵尸线程, 防资源泄漏 */
    player_active = 0; player_running = 0;
    fprintf(stderr, "[xr] 播放线程收尾 ok\n");
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
