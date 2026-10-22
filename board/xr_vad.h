/* xr_vad.h — 底噪标定、能量门、3 帧预滚、句子累积与端点判定.
 * 喂帧进、结果出; 时间与策略由调用方给, 模块内不含 main 的策略. */
#ifndef XR_VAD_H
#define XR_VAD_H
#include <stddef.h>
#include <stdint.h>
#include "xr_audio_format.h"

/* 预滚环容量(帧), 运行深度由 preroll_frames 决定(main 经 xr_vad_configure 注入,
   默认 5 帧=500ms, 对应 docs/EDGE-VOICE-PLAN.md P1.1 "保留约 0.5 秒防开头被截")。 */
#define XR_VAD_PREROLL_MAX 30
/* 重置底噪门后的快速重标定帧数(6 帧 = 600ms 内收敛到当前底噪) */
#define XR_VAD_RECAL_FRAMES 6

typedef struct {
    double noise_db;                     /* 底噪(-75 初值) */
    double base_db;                      /* 开机标定得到的底噪: reset_gate 回到这里 */
    int recal_left;                      /* >0: 空闲态用快速 alpha 重标定 */
    double margin, sustain_margin, trailing_s;   /* 6.0 / 2.0 / 0.5 */
    int min_voice_frames, utt_max_s;     /* 3 / 12 */
    int in_utt, voice_frames, quiet;
    int preroll_frames;                            /* 运行预滚深度(<=XR_VAD_PREROLL_MAX) */
    int16_t pre[XR_VAD_PREROLL_MAX][FRAME_SAMPLES]; /* 预滚环: 防开头半个字被吞 */
    int pre_n, pre_i;
    int16_t *utt; size_t utt_n, utt_cap; /* 句子缓冲(15s, 调用方分配) */
    double dbs[20]; int db_n;            /* 标定样本 */
} xr_vad_t;

typedef enum { XR_VAD_CONTINUE, XR_VAD_UTT_DONE, XR_VAD_TOO_SHORT } xr_vad_result_t;

void   xr_vad_init(xr_vad_t *v, int16_t *utt_buf, size_t cap);
/* 现场调参入口: 预滚帧数/触发余量/持续余量. 不调则用 init 默认(5/6.0/2.0) */
void   xr_vad_configure(xr_vad_t *v, int preroll_frames, double trigger_db, double sustain_db);
double xr_vad_frame_db(const int16_t *pcm, int n);
void   xr_vad_calibrate_feed(xr_vad_t *v, double db);
void   xr_vad_calibrate_finish(xr_vad_t *v);
double xr_vad_noise_db(const xr_vad_t *v);
void   xr_vad_clear_preroll(xr_vad_t *v);
void   xr_vad_reset_gate(xr_vad_t *v);
void   xr_vad_raise_gate(xr_vad_t *v);
/* *started=1 表示本帧开句. 能量门触发帧不跑端点判定, KWS 强制开句帧的端点判定
   由紧随其后的 xr_vad_feed 完成 —— 两者行为不同, 故用出参而非"开句"事件枚举 */
xr_vad_result_t xr_vad_feed(xr_vad_t *v, const int16_t *frame, int n, double db,
                            int *started);
/* 强制开句后必须再把同一帧喂一次 xr_vad_feed, 否则该帧的端点判定不会发生 */
xr_vad_result_t xr_vad_force_start(xr_vad_t *v, const int16_t *frame, int n,
                                   double db, int *started);
const int16_t *xr_vad_utterance(const xr_vad_t *v, size_t *n);
#endif