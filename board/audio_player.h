#ifndef XR_AUDIO_PLAYER_H
#define XR_AUDIO_PLAYER_H
#include <stddef.h>
#include <stdint.h>
typedef struct _snd_pcm snd_pcm_t;
/* Loaded ALSA operations, supplied once before the playback thread starts. */
typedef struct {
    int (*a_open)(snd_pcm_t **, const char *, int, int);
    int (*a_hwp_malloc)(void **);
    int (*a_hwp_any)(snd_pcm_t *, void *);
    int (*a_set_access)(snd_pcm_t *, void *, unsigned);
    int (*a_set_format)(snd_pcm_t *, void *, unsigned);
    int (*a_set_channels)(snd_pcm_t *, void *, unsigned);
    int (*a_set_rate)(snd_pcm_t *, void *, unsigned, int);
    int (*a_set_ps)(snd_pcm_t *, void *, unsigned long, int);
    int (*a_set_periods)(snd_pcm_t *, void *, unsigned, int);
    int (*a_hwp)(snd_pcm_t *, void *);
    void (*a_hwp_free)(void *);
    int (*a_prepare)(snd_pcm_t *);
    int (*a_writei)(snd_pcm_t *, const void *, unsigned long);
    int (*a_drop)(snd_pcm_t *);
    int (*a_close)(snd_pcm_t *);
    int (*a_drain)(snd_pcm_t *);
    const char *(*a_strerror)(int);
    /* 队列深度(帧, 设备采样率)。音乐路径用它做流量控制: 两台板子统一队列深度,
     * 同步才稳(实测两台 2.3s vs 0.1s 时轮间散布 3ms, 直出路径只有 0.07ms)。 */
    int (*a_delay)(snd_pcm_t *, long *frames);
} xr_audio_output_ops_t;

int xr_player_init(const xr_audio_output_ops_t *ops);
int xr_player_start(void);
void xr_player_finish(void);
int xr_player_finish_with_poll(int (*poll_callback)(void *), void *context);
void xr_player_drop(void);
void xr_player_push_pcm(const int16_t *pcm, size_t samples);
/* 音乐 PCM 入环(FIFO 线程专用)。语音期间丢弃, 不与语音抢环、不让 FIFO 堆积。 */
void xr_player_push_music_pcm(const int16_t *pcm, size_t samples);
/* FIFO 路径的音乐让位(整轮): 语音开始/结束由前端显式调用。
 *  - begin: 清音乐余量、置 paused(音乐生产者丢弃), 语音独占环(不套音乐预滚)
 *  - end  : 清语音尾巴、清 paused、置 music_prerolled=0 -> 重新预滚后再接回
 * 幂等; 没音乐会话时无动作。由前端在**第一句 TTS 音频到达**时 begin、
 * **离开 RESPOND** 时 end(思考期不暂停音乐)。 */
void xr_player_voice_begin(void);
void xr_player_voice_end(void);
int  xr_player_music_paused(void);
int xr_player_push_opus(const unsigned char *packet, size_t size);
size_t xr_player_queued_samples(void);
/* 音乐会话在位开关(Snapcast FIFO -> 播放环 用)。
 *
 * 置 1 后:
 *   - xr_player_start() 不清环、不重开声卡(音乐还在环里, 线程还在跑)
 *   - xr_player_finish() 只表示"这一句语音说完了", 不置 eof、不 join(否则把音乐一起收掉)
 *   - xr_player_drop()   只清环里的余量(音乐最多断 ~100ms, 随后由音乐生产者补上),
 *                        线程与声卡保持
 * 置 0 后恢复原来的行为。线程安全。
 */
void xr_player_set_music(int on);
int  xr_player_music_active(void);

/* 释放声卡句柄(所有权切换用): 停播放线程 + drain + close 设备, 句柄置空。
 * 之后 xr_player_start() 会自动重新打开(会话开始时用)。
 * 用途: 音乐由 snapclient 直出、要播语音时, 前端必须先真正让出声卡;
 * 现有的 finish/drop 都是"常驻不关", 对端永远拿不到 hw(实测 EBUSY)。 */
void xr_player_release_device(void);

/* 软件音量(1-100; -1/0=直通)。线程安全, 下一批 writei(≤120ms)生效。 */
void xr_player_set_volume(int pct);
int xr_player_volume(void);
#endif
