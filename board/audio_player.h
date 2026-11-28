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
/* Snapcast PCM 与语音分开排队，由同一播放线程按 ALSA 时钟混合。 */
void xr_player_push_music_pcm(const int16_t *pcm, size_t samples);
size_t xr_player_music_queued_samples(void);
/* KWS/语音回合期间只压低本机音乐，不停 Snapclient、不改变 Snapcast 组状态。
 * voice_begin 在声学唤醒/TTS 开始时调用，voice_end 在回合结束时恢复音乐增益。 */
void xr_player_voice_begin(void);
void xr_player_voice_end(void);
int  xr_player_music_ducked(void);
int  xr_player_music_duck_percent(void);
int xr_player_push_opus(const unsigned char *packet, size_t size);
size_t xr_player_queued_samples(void);
/* 音乐会话在位开关(Snapcast FIFO -> 独立音乐环 -> 同步混音 用)。
 *
 * 置 1 后:
 *   - xr_player_start() 不清环、不重开声卡(音乐还在环里, 线程还在跑)
 *   - xr_player_finish() 只表示"这一句语音说完了", 不置 eof、不 join(否则把音乐一起收掉)
 *   - xr_player_drop()   只清语音环余量，音乐环/播放线程/声卡保持
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
/* 独立语音通道增益(1-100%), 不改变 Snapcast 音乐电平或总音量设置。 */
void xr_player_set_voice_gain(int pct);
#endif
