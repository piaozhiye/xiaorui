/* xr_audio_format.h — 采集侧音频格式常量, 供 main/speech_pipeline/xr_vad 共用.
 * 宏名与取值与拆分前逐字一致, 仅为从 xiaorui_live.c 移出. */
#ifndef XR_AUDIO_FORMAT_H
#define XR_AUDIO_FORMAT_H
#define RATE 16000
#define FRAME_MS 100
#define FRAME_SAMPLES (RATE * FRAME_MS / 1000)
#endif