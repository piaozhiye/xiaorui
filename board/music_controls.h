#ifndef XR_MUSIC_CONTROLS_H
#define XR_MUSIC_CONTROLS_H
#include <stddef.h>
#include <stdint.h>
#include "xr_volume.h"

typedef enum {
    XR_MUSIC_NONE, XR_MUSIC_VOLUME, XR_MUSIC_STOP,
    /* 不是控制句(开灯/报时/问天气...): 播放期麦克风归本线程, 直接丢掉等于
       黑洞(2026-10-21 板上实测: 音乐播放中喊"开灯" -> "非控制语音忽略" ->
       HA 意图根本收不到)。改为把剥掉唤醒词后的正文交回主循环走正常轮。 */
    XR_MUSIC_FORWARD
} xr_music_action_t;
typedef struct {
    xr_music_action_t action;
    xr_vol_op_t volume_op;
    int value;
    char text[256];   /* XR_MUSIC_FORWARD: 剥掉唤醒词后的正文 */
} xr_music_command_t;
typedef struct {
    long (*read)(void *context, int16_t *pcm, size_t samples);
    int (*kws_feed)(const int16_t *pcm, int samples);
    void (*kws_reset)(void);
    int (*recognize)(const int16_t *, size_t, char *, size_t);
} xr_music_control_ops_t;

/* Separate capture owner for optional, strict playback controls (USB only).
 * read must return in <=100ms; stop joins before main touches KWS/ASR/capture. */
int xr_music_controls_start(const xr_music_control_ops_t *ops, void *context, int window_ms);
void xr_music_controls_stop(void);
int xr_music_controls_poll(xr_music_command_t *command);
int xr_music_controls_capturing(void);
/* 控制线程是否在跑(避免每句 TTS_START 重复 drop 设备打断 KWS 流)。 */
int xr_music_controls_running(void);
/* Call only after acoustic KWS evidence. Never routes general speech to LLM. */
int xr_music_command_parse(const char *text, xr_music_command_t *command);
#endif
