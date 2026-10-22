/* speech_pipeline.h — 声学 KWS + 离线 RKNN 精修 + CLI 回退 (从 xiaorui_live.c 抽出).
 * 模块只负责"检测/识别"; 要不要唤醒、要不要提交由调用方决定. */
#ifndef XR_SPEECH_PIPELINE_H
#define XR_SPEECH_PIPELINE_H
#include <stddef.h>
#include <stdint.h>

/* 可注入依赖: 板上默认用 dlopen/dlsym/popen; 主机测试注入 fake 实现.
 * open_lib 失败返 NULL; sym 失败返 NULL(实现负责打印 dlerror);
 * cli_refine 跑完一次 CLI 返 0(文本可能为空), 无法执行(写 wav/起进程失败)返 -1. */
typedef struct {
    void *(*open_lib)(void);
    void *(*sym)(void *lib, const char *name);
    int (*cli_refine)(const int16_t *pcm, size_t n, char *out, size_t cap);
} xr_speech_ops_t;

typedef struct {
    int local_asr;            /* 0 disables offline symbols, models AND CLI */
    const char *kws_dir;      /* NULL = existing RK3576 model directory */
    const char *library_path; /* NULL = libsherpa-onnx-c-api.so */
} xr_speech_config_t;
/* Non-NULL strings must remain valid until xr_speech_destroy(). */

const xr_speech_ops_t *xr_speech_default_ops(void);
int  xr_speech_init(const xr_speech_ops_t *ops);  /* dlopen 失败 -1(致命); ops NULL=默认 */
int  xr_speech_init_config(const xr_speech_ops_t *ops, const xr_speech_config_t *config);
int  xr_speech_kws_available(void);
int  xr_speech_kws_feed(const int16_t *pcm, int n); /* 1=本帧命中(已含内部 reset) */
int  xr_speech_kws_flush(void);                    /* 句尾收割+reset; 1=命中 */
void xr_speech_kws_reset(void);                    /* 不可用时空操作 */
int  xr_speech_recognize(const int16_t *pcm, size_t n, char *out, size_t cap);
void xr_speech_destroy(void);
#endif
